import math
import torch
import torch.distributed as dist
from torch._utils import _flatten_dense_tensors, _unflatten_dense_tensors
from collections import defaultdict

from deepspeed.runtime.zero.utils import _initialize_parameter_parallel_groups
from deepspeed.runtime.fp16.loss_scaler import LossScaler, DynamicLossScaler
from deepspeed.runtime.utils import get_grad_norm, CheckOverflow
from deepspeed.runtime.zero.config import ZERO_OPTIMIZATION_OPTIMIZER_STATES
from deepspeed.utils import logger, log_dist


def get_alignment_padding(flattened_lean_size, sub_partition_id, sub_partition_size):
    sub_partition_high_limit = (sub_partition_id + 1) * sub_partition_size
    if sub_partition_high_limit <= flattened_lean_size:
        return 0
    else:
        return min(sub_partition_size, sub_partition_high_limit - flattened_lean_size)


def get_group_alignment_padding(tensor_list, sub_partition_size, sub_partition_count):
    group_paddings = []
    flattened_size = sum([tensor.numel() for tensor in tensor_list])
    for i in range(sub_partition_count):
        padding = get_alignment_padding(flattened_size, i, sub_partition_size)
        group_paddings.append(padding)

    return group_paddings


def flatten_dense_tensors_sub_partition_aligned(tensor_list,
                                                dp, # s_note: 数据并行进程数
                                                max_elements_per_comm,
                                                pg):
    assert max_elements_per_comm >= dp, f"max_elements_per_comm {max_elements_per_comm} < dp {dp}"

    num_elements = sum(t.numel() for t in tensor_list)
    log_dist("Total number of elements in model: {}, max elements per com: {}".format(
        num_elements,
        max_elements_per_comm),
             ranks=[0])

    # Compute aligned partition size based on parameter count
    aligned_param_partition_size = math.ceil(num_elements / dp)

    # Compute aligned partition size based on communication size
    aligned_comm_partition_size = int(max_elements_per_comm // dp)

    # s_note: 这里的子分区还要明确具体作用, 有最大总通信量( max_elements_per_comm )的限制,
    if aligned_param_partition_size <= aligned_comm_partition_size:
        sub_partition_count = 1
        sub_partition_size = aligned_param_partition_size
    else:
        sub_partition_count = math.ceil(aligned_param_partition_size /
                                        aligned_comm_partition_size)
        sub_partition_size = aligned_comm_partition_size

    # Compute required padding  for alignment to dp and max_elements_per_comm
    padding = (sub_partition_count * sub_partition_size * dp) - num_elements

    log_dist(
        f"sub_partition_count: {sub_partition_count}, sub_partition_size: {sub_partition_size}, padding: {padding}",
        ranks=[0])
    log_dist(
        f"number of elements with padding: {num_elements} + {padding} = {num_elements + padding}",
        ranks=[0])

    if padding == 0:
        aligned_tensor_list = tensor_list
    else:
        pad_tensor = torch.zeros(padding,
                                 device=tensor_list[0].device,
                                 dtype=tensor_list[0].dtype)
        aligned_tensor_list = tensor_list + [pad_tensor]

    flat_tensors = _flatten_dense_tensors(aligned_tensor_list) # s_note: 把所有参数都reshape成一维, 然后conate在一起, 变成一个一维数组
    return flat_tensors


# s_note: 判断tensor和这个sub partition的inverval是否相交
def _single_range_check(current_index, start_index, end_index, tensor_size):
    # s_note: offset指交集的发生的位置距离current_index的offset
    offset = 0
    if (current_index >= start_index) and (current_index < end_index):
        # Fully inside bounds
        # s_note: tensor的起点在这个interval，前进0就到达
        return True, offset
    elif (start_index > current_index) and (start_index < (current_index + tensor_size)):
        # Partially contained, compute offset
        # s_note: sub partition的起点在tensor 的 interval，需要前进一个差值
        offset = start_index - current_index
        return True, offset
    else:
        return False, offset


# s_note: current_index是当前tensor的起点index
# 返回该参数关联的本rank所有要reduce的sub-partition
def _range_check(current_index, element_intervals, tensor_size):
    results = []
    # s_note: 遍历当前rank的各次通信的sub_partition的interval
    for comm_idx, interval in enumerate(element_intervals):
        start_index, end_index = interval
        # s_note: 判断是否有交集，以及交集的发生的位置距离current_index的offset
        contained, offset = _single_range_check(current_index, start_index, end_index, tensor_size)
        if contained:
            # s_note: 当前tensor在本rank关联的所有通信以及这次通信时，tensor对应数据的起始位置需要相对tensor初始位置current_index的偏移量
            results.append((contained, offset, comm_idx))
    # s_note: 本rank不要reduce该参数
    if len(results) == 0:
        return [(False, 0, -1)]
    return results


class FP16_DeepSpeedZeroOptimizer_Stage1(object):
    """
    FP16_DeepSpeedZeroOptimizer_Stage1 designed to reduce the memory footprint
    required for training large deep learning models.

    For more details please see ZeRO: Memory Optimization Towards Training A Trillion Parameter Models
    https://arxiv.org/abs/1910.02054

    This version aligns with stage-1 in the paper above.
    """
    def __init__(self,
                 init_optimizer,
                 static_loss_scale=1.0,
                 dynamic_loss_scale=False,
                 dynamic_loss_args=None,
                 verbose=True,
                 dp_process_group=None,
                 partition_size=None,  # s_note: Engine处这里没有赋值
                 mpu=None,
                 all_gather_partitions=True,
                 allgather_size=500000000,
                 clip_grad=0.0,
                 max_elements_per_comm=5e8,
                 elastic_checkpoint=True):

        if dp_process_group is not None and partition_size is not None:
            raise ValueError("Cannot specify both dp_process_group "
                             "and partition size")

        if dp_process_group is None:
            dp_process_group = _initialize_parameter_parallel_groups(partition_size)

        if not torch.cuda.is_available:
            raise SystemError("Cannot use fp16 without CUDA.")
        self.optimizer = init_optimizer

        self.verbose = verbose
        # s_note: 使用的Engine传入的
        self.dp_process_group = dp_process_group

        # TODO: automatically turn off if #params > some_limit
        self.all_gather_partitions = all_gather_partitions
        self.allgather_size = allgather_size

        # self.max_elements_per_comm = max_elements_per_comm
        # logger.info("max_elements_per_comm={}".format(max_elements_per_comm))

        self.elastic_checkpoint = elastic_checkpoint
        logger.info(f'ZeRO Elastic Checkpoint = {elastic_checkpoint}')

        # param flattened by groups
        self.fp16_groups = []
        self.fp16_groups_flat = []

        # Setup bookkeeping data structures depending on partitioning type

        # parallel_sub_partitioned_fp16_groups[group-idx] -> [comm-ids] -> [rank-ids]
        self.parallel_sub_partitioned_fp16_groups = []
        # same underlying data as above but viewed as: [groups] -> [rank-ids] -> [comm-ids]
        self.parallel_comm_sub_partitioned_fp16_groups = []

        # 32-bit sub-partitions of the parallel partitioned parameters
        # that this process will update
        self.local_sub_partitions_of_fp32_groups = []

        # param partition info

        # parameters in each group that will not be updated by this process directly
        self.params_not_local = []

        # parameters that will be updated by this process directly
        self.params_in_rank_sub_partitions = []

        # parameter offsets for parameters in sub-partitions. Parameter
        # boundaries may not align with sub-partition boundaries
        # so we need to keep track of the offsets
        self.params_in_rank_sub_partitions_offsets = []

        # number of elements per sub-partition in each group
        self.sub_partition_sizes = []

        # number of communication intervals for each group
        self.num_comm_intervals_per_group = []

        local_rank = dist.get_rank(group=self.dp_process_group)

        self.group_paddings = []
        self.partition_count = dist.get_world_size(group=self.dp_process_group)

        self.default_device = self.optimizer.param_groups[0]['params'][0].device

        # max elems per param group
        self.max_elems_per_comm = []

        # loop to deal with groups
        for i, param_group in enumerate(self.optimizer.param_groups):
            # s_note: 什么是para_goups？
            # 参考：https://pytorch.org/docs/stable/optim.html 和 https://zhuanlan.zhihu.com/p/87209990
            # optimizer初始化时传入多组params，被放到param_groups这里List里面
            # optim.SGD([
            #    {'params': model.base.parameters()},
            #    {'params': model.classifier.parameters(), 'lr': 1e-3}
            #    ], lr=1e-2, momentum=0.9)
            #
            # param_groups = List[Dict[{'params': params_iter}, ...]]
            #
            # for group in self.param_groups:
            #     # group 是个dict，其中有个key为params对应参数的iter
            #     weight_decay = group['weight_decay'] # 里面存了多组参数
            #     momentum = group['momentum']
            #     dampening = group['dampening']
            #     nesterov = group['nesterov']
            #     # 遍历参数的iter，就拿到了参数tensor和其grad
            #     for p in group['params']:
            #         p 是optimizer关联的parameter tensor
            #         p.grad 是其梯度tensor
            # push this group to list before modify
            # s_note: fp16_groups中有完整的fp16 parameters
            self.fp16_groups.append(param_group['params'])

            # calculate best max elements per comm based to minimize padding
            # s_note: 该parameter tensor一次通信最大的element个数
            self.max_elems_per_comm.append(
                self.best_max_elems_per_comm(
                    num_elements=sum(t.numel() for t in self.fp16_groups[i]),  # 一个para_group里面所有参数tensor里面的基础数据元素(fp16)个数总数
                    max_elements_per_comm=max_elements_per_comm,  # 一次通信最大的基础数据元素个数
                    dp=dist.get_world_size(group=self.dp_process_group)  # 进程数 or 卡数
                ))

            # flattens all tensors into single 1d tensor aligned with sub-partition size for later dividing
            # RS: create aligned sub-partitions
            # s_note: 把所有参数打平成1d的tensor，并按sub-partition做对齐
            flat_aligned_params = flatten_dense_tensors_sub_partition_aligned(
                tensor_list=self.fp16_groups[i],
                dp=dist.get_world_size(group=self.dp_process_group),
                max_elements_per_comm=self.max_elems_per_comm[i],
                pg=self.dp_process_group)
            self.fp16_groups_flat.append(flat_aligned_params)

            # TODO: I don't think this does anything?
            # set model fp16 weight to slices of flattened buffer
            # s_note: 这里把flatten成一维的参数又根据 self.fp16_groups[i] 里面的参数信息,
            #         恢复成原来的张量大小, 感觉操作是多余的, 他们自己的注释也说了
            # s_note: updated_params是fp16_groups_flat按fp16_groups的shape创建的view
            updated_params = _unflatten_dense_tensors(self.fp16_groups_flat[i],
                                                      self.fp16_groups[i])
            for p, q in zip(self.fp16_groups[i], updated_params):
                # s_note: p这个parameter or variable的tensor（.data获取的）被替换为q的tensor
                # .data接口：https://stackoverflow.com/questions/51743214/is-data-still-useful-in-pytorch
                # 把fp16参数tensor映射到flat数据上
                p.data = q.data

            # divide the flat weights into near equal partition equal to the data parallel degree
            # each process will compute on a different part of the partition
            # RS: split into two layer list -> [comm-id] -> [sub-partitions per rank]
            # s_note: 对每个parameter，先按单次通信最大size切，然后按world_size再切，二维切分
            # comm_id代表通信的编号，rank代表进程编号，sub_partition代表一次通信中对应进程数量个分片中的一片
            # comm_partitions, [comm_id] -> List[sub_partition]，一次通信对应的多个分片
            # dp_sub_partitions, [rank] -> List[sub_partition]，一个进程多次通信，每次要reduce的分片
            # element_intervals, [rank] -> [(start_idx, end_idx), (start_idx, end_idx), ...]，对应dp_sub_partitions在flat数据中的index
            # sub_partition_size，一个子partition的大小
            # num_comm_intervals，通信次数
            comm_partitions, dp_sub_partitions, element_intervals, sub_partition_size, num_comm_intervals = \
                self.get_data_parallel_sub_partitions(
                    tensor=self.fp16_groups_flat[i],
                    max_elements_per_comm=self.max_elems_per_comm[i],
                    world_size=dist.get_world_size(
                        group=self.dp_process_group),
                    dp_process_group=self.dp_process_group
                )
            self.parallel_comm_sub_partitioned_fp16_groups.append(
                comm_partitions)  # comm -> rank
            self.parallel_sub_partitioned_fp16_groups.append(
                dp_sub_partitions)  # rank -> comm
            self.sub_partition_sizes.append(sub_partition_size)
            self.num_comm_intervals_per_group.append(num_comm_intervals)
            # data_parallel_partitions = self.get_data_parallel_partitions(self.fp16_groups_flat[i])
            # self.parallel_partitioned_fp16_groups.append(data_parallel_partitions)

            # a partition of the fp32 master weights that will be updated by this process
            # RS: store/detach/cast our local sub-partitions
            # s_note: 本进程需要reduce的参数分片
            local_sub_partitions = []
            for sub_partition in self.parallel_sub_partitioned_fp16_groups[i][
                    local_rank]:
                # 创建了fp16分片对应的fp32分片，用于update
                fp32_sub_partition = sub_partition.clone().float().detach()
                fp32_sub_partition.requires_grad = True
                local_sub_partitions.append(fp32_sub_partition)
            # s_notes: 记录本进程需要更新的fp32参数分片
            self.local_sub_partitions_of_fp32_groups.append(local_sub_partitions)

            # Compute sub_partition paddings
            sub_partition_paddings = get_group_alignment_padding(
                tensor_list=self.fp16_groups[i],
                sub_partition_size=sub_partition_size,
                sub_partition_count=num_comm_intervals * self.partition_count)
            self.group_paddings.append(sub_partition_paddings)

            # modify optimizer of have flat master weight
            # self.local_partition_of_fp32_groups[i].requires_grad = True # keep this in case internal optimizer uses it
            # s_note: 这里应该是self.local_sub_partitions_of_fp32_groups[i].requires_grad = True # keep this in case internal optimizer uses it
            # s_note: 这里 self.optimizer.param_groups 中的 params 被本地的fp32分片替换了
            #         而 self.fp16_groups 中保存着原来 self.optimizer 中的 fp16 param_group['params']
            param_group['params'] = self.local_sub_partitions_of_fp32_groups[i]

            # RS: divide up the sub-partitions and keep track of offsets for each param
            # partition_size = len(self.fp16_groups_flat[i]) / dist.get_world_size(group=self.dp_process_group)
            # s_note: 记录每个rank的每次通信的每个分片关联的parameter及其通信时取数据相对tensor起点的offset
            params_in_rank_sub_partition, params_in_rank_sub_partitions_offsets, params_not_local = self.get_all_sub_partition_info(
                tensor_list=self.fp16_groups[i],
                all_element_intervals=element_intervals,
                local_rank=local_rank,
                world_size=dist.get_world_size(group=self.dp_process_group)
            )

            self.params_in_rank_sub_partitions.append(params_in_rank_sub_partition)
            self.params_not_local.append(params_not_local)
            self.params_in_rank_sub_partitions_offsets.append(
                params_in_rank_sub_partitions_offsets)

        # we may have a way of fusing dynamic scale. Do not support for now
        if dynamic_loss_scale:
            if dynamic_loss_args is None:
                self.loss_scaler = DynamicLossScaler()
            else:
                self.loss_scaler = DynamicLossScaler(**dynamic_loss_args)

            self.dynamic_loss_scale = True

        else:
            self.dynamic_loss_scale = False
            self.loss_scaler = LossScaler(scale=static_loss_scale)
            self.cur_iter = 0

        self.mpu = mpu
        self.clip_grad = clip_grad

        self.overflow = False
        self.overflow_checker = CheckOverflow(self.fp16_groups,
                                              mpu=self.mpu,
                                              zero_reduce_scatter=True)

        self._initialize_optimizer_states()

    def _initialize_optimizer_states(self):
        for group_idx, group in enumerate(self.local_sub_partitions_of_fp32_groups):
            for idx, sub_partition_param in enumerate(group):
                sub_partition_grad = torch.zeros(int(
                    self.sub_partition_sizes[group_idx]),
                                                 dtype=sub_partition_param.dtype).cuda()
                sub_partition_param.grad = sub_partition_grad

        # s_note: 这里调用到最底层的 adam 优化器, 会根据 optimizer 中 group['params'] 
        #         的参数大小对应初始化 m 和 v, 这里也就完成了 m 和 v 的分片
        self.optimizer.step()

        for group in self.local_sub_partitions_of_fp32_groups:
            for idx, sub_partition_param in enumerate(group):
                sub_partition_param.grad = None

    @staticmethod
    def best_max_elems_per_comm(num_elements, # s_note: 模型总参数量大小, 指元素个数不是字节数
                                max_elements_per_comm, # s_note: 默认值 5e8, 5千万
                                dp # s_note: 数据并行进程数
                                ): 
        # if we use max-elems-per-comm as is, how many comm intervals will there be
        # s_note: 最大可能的通信次数，上取整
        max_comm_intervals = math.ceil(num_elements / max_elements_per_comm)
        # s_note: 此时，最后一次通信需要padding，padding的元素数量
        padding_for_max_comm = (max_elements_per_comm *
                                max_comm_intervals) - num_elements

        # if we use 1 less comm interval how much extra comm padding would be required
        # s_note: 下取整，少一次通信，// 表示python 3的整数除法，/ 表示python 3的浮点除法
        # 每次通信略超出上限的数量
        min_comm_intervals = num_elements // max_elements_per_comm
        if min_comm_intervals == 0:
            # 下取整为0，表示本来就需要一次通信
            log_dist(f'Using default max_elements_per_comm {max_elements_per_comm}',
                     ranks=[0])
            # 此时返回传入的单次最大通信大小
            # 只需一次通信
            return max_elements_per_comm

        # s_note: 现在需要至少2次通信
        # dp代表一次通信分块进程数量块
        # 上取整
        # 每次通信的每个分块都padding一个元素
        # num_elements / min_comm_intervals 表示现在需要的实际每次通信大小m
        # m再除以dp得到x，x是每个分片的实际大小，这是有小数部分的数
        # x上取整表示一个分片的实际大小
        # z = x * (dp * min_comm_intervals) - num_elements
        # z才是padding的数量
        # s_quest: 这块没看懂？
        padding_for_min_comm = math.ceil(num_elements / (dp * min_comm_intervals))

        # choose padding that uses least amount of overhead
        if padding_for_max_comm > padding_for_min_comm:
            new_max_elements_per_comm = padding_for_min_comm + max_elements_per_comm
            log_dist(
                f'Updating max_elements_per_comm from {max_elements_per_comm} -> {new_max_elements_per_comm}',
                ranks=[0])
            return new_max_elements_per_comm
        else:
            log_dist(f'Using default max_elements_per_comm {max_elements_per_comm}',
                     ranks=[0])
            return max_elements_per_comm

    @staticmethod
    def get_data_parallel_sub_partitions(tensor,
                                         max_elements_per_comm,
                                         world_size,
                                         dp_process_group=None):
        total_num_elements = tensor.numel()

        # if total elements is less than our max, revert to splitting into dp partitions

        max_elements_per_comm = min(total_num_elements, max_elements_per_comm)
        sub_partition_size = int(max_elements_per_comm // world_size)

        # Ensure partition alignment was done correctly
        # s_note: 按照这里的逻辑, num_sub_partitions 是有可能大于数据并行进程数也就是设备数的
        #         因为如果 total_num_elements > max_elements_per_comm, 
        #         那么  sub_partition_size < int(total_num_elements // world_size)
        #         则 num_sub_partitions > (total_num_elements / int(total_num_elements // world_size)) = world_size
        num_sub_partitions = int(total_num_elements // sub_partition_size)
        assert total_num_elements % sub_partition_size == 0, "{} % {} != 0".format(total_num_elements, sub_partition_size)

        # Ensure comm interval alignment was done correctly.
        num_comm_intervals = int(num_sub_partitions // world_size)
        assert num_sub_partitions % world_size == 0, "{} % {} != 0".format(num_sub_partitions, world_size)

        if not dist.is_initialized() or dist.get_rank(group=dp_process_group) == 0:
            logger.info("**** partition info:")
            logger.info("\t total_num_elements=%s", total_num_elements)
            logger.info("\t world_size=%s", world_size)
            logger.info("\t max_elements_per_comm=%s", max_elements_per_comm)
            logger.info("\t sub_partition_size=%s", sub_partition_size)
            logger.info("\t num_sub_partitions=%s", num_sub_partitions)
            logger.info("\t num_comm_intervals=%s", num_comm_intervals)
            logger.info("****")

        # [comm_id] -> [rank]
        comm_partitions = []
        for _ in range(num_comm_intervals):
            comm_partitions.append([])

        start = 0
        comm_id = 0
        element_intervals = defaultdict(
            list)  # [rank] -> [(start,end), (start,end), ...]

        # s_note: 
        #
        # 首先让 n + 1 表示进程数也就是 world_size, sps 表示 sub_partition_size:
        #
        # element_intervals = {
        #   rank_0 : [(0, sps), (sps * (n + 1), sps * (n + 2)), ...],
        #   rank_1 : [(sps, sps * 2), (sps * (n + 2), sps * (n + 3)), ...],
        #    .....
        #   rank_n : [(sps * n, sps * (n + 1)), (sps * (n + n + 1), sps * (n + n + 2)), ...]
        # }
        #
        #
        # comm_partitions = [
        #   comm_id_0 -> [tensor(0, sps), tensor(sps, sps * 2), ..., tensor(sps * n, sps * (n + 1))],
        #   comm_id_1 -> [tensor(sps * (n + 1), sps * (n + 2)), tensor(sps * (n + 2), sps * (n + 3)), ..., tensor(sps * (n + n + 1), sps * (n + n + 2))],
        #   ......
        # ]
        #
        for idx in range(num_sub_partitions):
            rank_id = idx % world_size
            sub_partition = tensor.narrow(0, start, sub_partition_size).detach()
            element_intervals[rank_id].append((start, start + sub_partition_size))
            comm_partitions[comm_id].append(sub_partition)
            start = start + sub_partition_size
            if rank_id == (world_size - 1):
                comm_id += 1

        # [rank] -> [comm_id]
        sub_partitions = []
        for _ in range(world_size):
            sub_partitions.append([])

        # s_note:
        # sub_partitions = {
        #   rank_0 -> [tensor(0, sps), tensor(sps * (n + 1), sps * (n + 2)), ...],
        #   rank_1 -> [tensor(sps, sps * 2), tensor(sps * (n + 2), sps * (n + 3)), ...],
        #    .....
        #   rank_n -> [tensor(sps * n, sps * (n + 1)), tensor(sps * (n + n + 1), sps * (n + n + 2)), ...]
        # }
        for comm_id, partitions in enumerate(comm_partitions):
            for rank_id, partition in enumerate(partitions):
                sub_partitions[rank_id].append(partition)

        return comm_partitions, sub_partitions, element_intervals, sub_partition_size, num_comm_intervals

    @staticmethod
    def get_all_sub_partition_info(tensor_list, # s_note: 一个参数group的 fp16 参数list
                                   all_element_intervals, # s_note: 从 self.get_data_parallel_sub_partitions 函数返回，一个rank每次通信要reduce的对应分片的起始index
                                   local_rank,
                                   world_size):
        params_not_local = []

        # [rank] -> [comm-id] -> [param/offset]
        params_in_rank_sub_partition = []
        params_in_rank_sub_partitions_offsets = []

        # s_note: 对于每个进程
        for rank in range(world_size):
            params_in_local_sub_partition = []
            local_sub_partition_offsets = []
            comm_tensor_list = []
            comm_offset_list = []
            current_index = 0
            prev_comm_idx = 0
            # s_note: 对于当前参数group中的每个参数
            for iii, tensor in enumerate(tensor_list):
                tensor_size = tensor.numel()
                #if local_rank == 0:
                #    # logger.info("rank={}, current_index={}, tensor_size={}, tensor-idx={}".format(rank,
                #        current_index, tensor_size, iii))
                # s_note: 本参数关联的rank的sub_partitions（以comm_idx和offset标识）
                results_list = _range_check(current_index,
                                            all_element_intervals[rank],
                                            tensor_size)
                for contained, offset, comm_idx in results_list:
                    #if local_rank == 0:
                    #    logger.info("rank={}, contained={}, offset={}, comm_idx={}".format(rank, contained,
                    #        offset, comm_idx))
                    if contained:
                        if prev_comm_idx != comm_idx:
                            # s_note: 进入下次通信
                            params_in_local_sub_partition.append(comm_tensor_list)
                            comm_tensor_list = []
                            local_sub_partition_offsets.append(comm_offset_list)
                            comm_offset_list = []
                        # s_note: 当前通信相关的
                        comm_tensor_list.append(tensor)
                        comm_offset_list.append(offset)
                        prev_comm_idx = comm_idx
                    elif rank == local_rank:
                        # s_note: 该参数本rank无需更新
                        params_not_local.append(tensor)

                current_index = current_index + tensor_size

            #assert len(comm_tensor_list) > 0
            #assert len(comm_offset_list) > 0
            # comm -> list of parameter
            # s_note: rank关联的每次通信要reduce grad的参数list
            params_in_local_sub_partition.append(comm_tensor_list)
            # s_note: 关联参数grad通信时距离参数起点index的offset
            local_sub_partition_offsets.append(comm_offset_list)

            # rank -> comm
            params_in_rank_sub_partition.append(params_in_local_sub_partition)
            params_in_rank_sub_partitions_offsets.append(local_sub_partition_offsets)

        return params_in_rank_sub_partition, params_in_rank_sub_partitions_offsets, params_not_local

    # 返回的flat sub partition grad
    @staticmethod
    def get_flat_sub_partitions(comm_tensor_list,
                                comm_param_offsets,
                                sub_partition_size,
                                dtype,
                                default_device,
                                num_comm_intervals=None,
                                return_partition_params=False):

        partition_params = []
        final_param_offsets = []
        flat_sub_partitions = []
        for tensor_list, param_offsets in zip(comm_tensor_list, comm_param_offsets):
            flat_tensor_list = []
            current_size = 0
            my_offsets = []
            my_params = []

            for i, tensor in enumerate(tensor_list):
                if tensor.grad is None:
                    tensor.grad = torch.zeros(tensor.size(),
                                              dtype=tensor.dtype,
                                              device=tensor.device)
                # s_note: 参数
                param = tensor
                # s_note: 参数的grad
                tensor = tensor.grad
                num_elements = tensor.numel()
                tensor_offset = 0

                #we need to offset to get to the right element
                if i == 0 and param_offsets[i] > 0:
                    tensor_offset = param_offsets[i]
                    num_elements = num_elements - tensor_offset

                # We don't need all elements of the tensor if this tensor is
                # larger than we have space for in our curr sub-partition
                if num_elements > (sub_partition_size - current_size):
                    num_elements = sub_partition_size - current_size

                #we need a narrow view of the tensor based on the tensor offset and number of elements that
                #we need from this tensor
                if tensor_offset > 0 or num_elements < tensor.numel():
                    # s_note: 参数的梯度flatten
                    # s_note: tesnor.view, 数据共享，解释方式不同 https://pytorch.org/docs/stable/tensor_view.html
                    # s_note: 裁剪出该para的grad在该sub_partiton的部分
                    flat_tensor_list.append(tensor.contiguous().view(-1).narrow(
                        0,
                        int(tensor_offset),
                        int(num_elements)).to(dtype))
                else:
                    flat_tensor_list.append(tensor.to(dtype))
                my_params.append(param)

                #remember offset into partition and #elems for this tensor
                my_offsets.append((current_size, num_elements))

                current_size = current_size + num_elements

            #this means its the last partition and does not align with the dp boundary. We need to pad before flattening
            if current_size < sub_partition_size:
                my_offsets.append((None, None))
                my_params.append(None)
                if len(tensor_list) == 0:
                    assert default_device != None
                    flat_tensor_list.append(
                        torch.zeros(int(sub_partition_size - current_size),
                                    dtype=dtype,
                                    device=default_device))
                else:
                    flat_tensor_list.append(
                        torch.zeros(int(sub_partition_size - current_size),
                                    dtype=dtype,
                                    device=tensor_list[0].device))
            partition_params.append(my_params)  #flat_tensor_list)
            final_param_offsets.append(my_offsets)
            assert len(flat_tensor_list) == len(my_offsets), "{} {}".format(len(flat_tensor_list), len(my_offsets))
            # s_note: flat sub partition grad
            flat_sub_partitions.append(_flatten_dense_tensors(flat_tensor_list))
        if num_comm_intervals is not None and len(
                flat_sub_partitions) < num_comm_intervals:
            # logger.info("padding w. sub partitions to ensure uniform communication")
            device = flat_sub_partitions[0].device
            for _ in range(num_comm_intervals - len(flat_sub_partitions)):
                flat_sub_partitions.append(
                    torch.zeros(int(sub_partition_size),
                                dtype=dtype,
                                device=device))
                partition_params.append([None])
                final_param_offsets.append([(None, None)])

        if return_partition_params:
            assert len(flat_sub_partitions) == len(partition_params)
            assert len(partition_params) == len(final_param_offsets), "{} {}".format(len(partition_params), len(final_param_offsets))
            return flat_sub_partitions, partition_params, final_param_offsets
        return flat_sub_partitions

    def zero_grad(self, set_grads_to_None=True):
        """
        Zero FP16 parameter grads.
        """
        # FP32 grad should never exist.
        # For speed, set model fp16 grad to None by default
        for group in self.fp16_groups:
            for p in group:
                if set_grads_to_None:
                    p.grad = None
                else:
                    if p.grad is not None:
                        p.grad.detach_()
                        p.grad.zero_()

    def free_grad_in_param_list(self, param_list):
        for p in param_list:
            if isinstance(p, list):
                for _p in p:
                    _p.grad = None
            else:
                p.grad = None

    # s_note: loss.backward()之后的grad split & reduce-scatter
    def reduce_scatter_gradients(self,
                                 postscale_gradients,
                                 gradient_predivide_factor,
                                 gradient_average):
        world_size = dist.get_world_size(group=self.dp_process_group)
        local_rank = dist.get_rank(group=self.dp_process_group)

        for i, group in enumerate(self.fp16_groups):
            # s_note: 对于第i个参数
            # s_note: 获取其需要的通信次数
            num_comm_intervals = self.num_comm_intervals_per_group[i]

            # 遍历world_size个进程
            # 记录该参数的所有grad分片的view
            all_sub_partitions = []
            for rank in range(world_size):
                # gsp is list of partitions indexed by comm_idx
                # s_note: 获取本进程本参数现有的 fp16 梯度分片的view
                # s_note: comm -> fp16 sub partitions
                grad_sub_partitions = self.get_flat_sub_partitions(
                    comm_tensor_list=self.params_in_rank_sub_partitions[i][rank],
                    comm_param_offsets=self.params_in_rank_sub_partitions_offsets[i]
                    [rank],
                    dtype=torch.half,
                    default_device=self.default_device,
                    sub_partition_size=self.sub_partition_sizes[i],
                    num_comm_intervals=self.num_comm_intervals_per_group[i])
                # rank -> comm
                all_sub_partitions.append(grad_sub_partitions)

                assert len(grad_sub_partitions) == num_comm_intervals

            local_comm_partitions = []
            # s_note: 分为 num_comm_intervals 次通信
            for comm_idx in range(num_comm_intervals):
                single_comm_all_partitions = []
                for rank in range(world_size):
                    # s_note: 汇总本次通信在本进程关联的多个grad分片
                    # s_note: rank -> grad sub partitions
                    single_comm_all_partitions.append(all_sub_partitions[rank][comm_idx])

                if postscale_gradients:
                    if gradient_predivide_factor != 1.0:
                        for partition in single_comm_all_partitions:
                            partition.mul_(1. / gradient_predivide_factor)

                    dist.reduce_scatter(output=single_comm_all_partitions[local_rank],
                                        input_list=single_comm_all_partitions,
                                        group=self.dp_process_group)

                    if gradient_average:
                        # Only need to average our local grads in post scaling
                        if gradient_predivide_factor != world_size:
                            single_comm_all_partitions[local_rank].mul_(
                                gradient_predivide_factor / world_size)
                else:
                    for partition in single_comm_all_partitions:
                        partition.div_(world_size)
                    # s_note: reduce_scatter 全局同步分发  fp16 梯度 
                    dist.reduce_scatter(output=single_comm_all_partitions[local_rank],
                                        input_list=single_comm_all_partitions,
                                        group=self.dp_process_group)

    # s_note: stage 1 parameter update
    def step(self, closure=None):
        # First compute norm for all group so we know if there is overflow
        self.overflow = self.overflow_checker.check()

        prev_scale = self.loss_scale
        self._update_scale(self.overflow)
        if self.overflow:
            self.zero_grad()
            if self.verbose:
                logger.info("[deepspeed] OVERFLOW! Skipping step. Attempted loss "
                            "scale: {}, reducing to {}".format(
                                prev_scale,
                                self.loss_scale))
            return self.overflow

        norm_groups = []
        local_sub_partitions_grad_groups = []

        partition_id = dist.get_rank(group=self.dp_process_group)
        # s_note: 对于每个group的参数
        for i, group in enumerate(self.fp16_groups):
            #TODO RS: update get grad norm to support sub partitions
            norm_groups.append(get_grad_norm(group, mpu=self.mpu))

            #RS: update free grads w.r.t. sub partitions
            #free gradients for all the parameters that are not updated by this process
            # s_note: 这里释放了 fp16 的梯度? 这应该是 stege2 才要做的事情?
            # s_note: 释放了本rank无需更新的参数的的fp16梯度
            # s_note: 但是只要有一个分片在本rank，这里就没有释放其它无需更新的分片
            self.free_grad_in_param_list(self.params_not_local[i])

            # create flat gradient partitions for parameters updated by this process
            # s_note: 本rank要更新的grad sub-partitons fp32
            local_grad_sub_partitions = self.get_flat_sub_partitions(
                comm_tensor_list=self.params_in_rank_sub_partitions[i][partition_id],
                comm_param_offsets=self.params_in_rank_sub_partitions_offsets[i]
                [partition_id],
                sub_partition_size=self.sub_partition_sizes[i],
                dtype=self.local_sub_partitions_of_fp32_groups[i][0].dtype,
                num_comm_intervals=self.num_comm_intervals_per_group[i],
                default_device=self.default_device)

            #RS: update all our local params with sub-partition grads
            for idx, sub_partition_param in enumerate(self.local_sub_partitions_of_fp32_groups[i]):
                sub_partition_param.grad = local_grad_sub_partitions[idx]

            #RS: update free grads for sub-partitions
            # release all the gradient since we have already created a necessary copy in dp_grad_partition
            # s_note: 这里释放了 fp16 的梯度? 这应该是 stege2 才要做的事情? 还有上面 dp_grad_partition 指什么?
            self.free_grad_in_param_list(
                self.params_in_rank_sub_partitions[i][partition_id])

            local_sub_partitions_grad_groups.append(local_grad_sub_partitions)

        #RS: update unscale/clip with sub partitions
        self.unscale_and_clip_grads(local_sub_partitions_grad_groups, norm_groups)

        self.optimizer.step()

        #RS: clear our sub partition grads
        #get rid of the fp32 gradients. Not needed anymore
        # s_note: 释放本地分片对应的 fp32 梯度
        for group in self.local_sub_partitions_of_fp32_groups:
            for idx, sub_partition_param in enumerate(group):
                sub_partition_param.grad = None
            #group.grad = None

        #NOTE RS: removed norm_groups outer loop from original code, i don't think it's needed
        #RS: copy all sub-partition fp32 data to fp16 sub partitions
        # copy fp32 param data to fp16 partitions w.r.t. our local rank
        for fp16_all_sub_partitions, fp32_local_sub_partitions in zip(self.parallel_sub_partitioned_fp16_groups, self.local_sub_partitions_of_fp32_groups):
            for local_sub_partition_param_fp16, local_sub_partition_param_fp32 in zip(fp16_all_sub_partitions[partition_id], fp32_local_sub_partitions):
                local_sub_partition_param_fp16.data.copy_(
                    local_sub_partition_param_fp32.data)

        #RS: all_gather/broadcast sub-partitions in separate comm calls
        #gather the updated weights from everyone
        # s_note: all_gather 获取全局更新之后的 fp16 梯度
        for fp16_all_sub_partitions in self.parallel_comm_sub_partitioned_fp16_groups:
            for comm_id, sub_partitions in enumerate(fp16_all_sub_partitions):
                dist.all_gather(sub_partitions,
                                sub_partitions[partition_id],
                                group=self.dp_process_group)

        # TODO: we probably don't need this? just to be safe
        for i in range(len(norm_groups)):
            updated_params = _unflatten_dense_tensors(self.fp16_groups_flat[i],
                                                      self.fp16_groups[i])
            for p, q in zip(self.fp16_groups[i], updated_params):
                p.data = q.data

        return self.overflow

    def unscale_and_clip_grads(self, grad_groups_flat, norm_groups):
        total_norm = 0.0
        for norm in norm_groups:
            total_norm += norm**2.0
        total_norm = math.sqrt(total_norm)

        # compute combined scale factor for this group
        combined_scale = self.loss_scale
        if self.clip_grad > 0.:
            # norm is in fact norm*scale
            clip = ((total_norm / self.loss_scale) + 1e-6) / self.clip_grad
            if clip > 1:
                combined_scale = clip * self.loss_scale

        for grad in grad_groups_flat:
            if isinstance(grad, list):
                sub_partitions = grad
                for g in sub_partitions:
                    g.data.mul_(1. / combined_scale)
            else:
                grad.data.mul_(1. / combined_scale)

    def backward(self, loss, retain_graph=False):
        self.loss_scaler.backward(loss.float(), retain_graph=retain_graph)

    def _update_scale(self, has_overflow=False):
        self.loss_scaler.update_scale(has_overflow)

    # Promote state so it can be retrieved or set via "fp16_optimizer_instance.state"
    def _get_state(self):
        return self.optimizer.state

    def _set_state(self, value):
        self.optimizer.state = value

    state = property(_get_state, _set_state)

    # Promote param_groups so it can be retrieved or set via "fp16_optimizer_instance.param_groups"
    # (for example, to adjust the learning rate)
    def _get_param_groups(self):
        return self.optimizer.param_groups

    def _set_param_groups(self, value):
        self.optimizer.param_groups = value

    param_groups = property(_get_param_groups, _set_param_groups)

    # Promote loss scale so it can be retrieved or set via "fp16_optimizer_instance.loss_scale"
    def _get_loss_scale(self):
        return self.loss_scaler.loss_scale

    def _set_loss_scale(self, value):
        self.loss_scaler.cur_scale = value

    loss_scale = property(_get_loss_scale, _set_loss_scale)
    cur_scale = property(_get_loss_scale, _set_loss_scale)

    # Return communication interval paddings for local rank and group
    def _get_local_group_paddings(self, group_index):
        local_rank = dist.get_rank(group=self.dp_process_group)
        sub_partition_indices = [
            local_rank + (comm_idx * self.partition_count)
            for comm_idx in range(self.num_comm_intervals_per_group[group_index])
        ]
        group_paddings = [
            self.group_paddings[group_index][sub_idx]
            for sub_idx in sub_partition_indices
        ]
        return group_paddings

    # Return group tensor after removing paddings that are added for alignment to DP world size.
    # This method works on the assumption that each group contains sub partitions.
    def _get_groups_without_padding(self, groups_with_padding):
        groups_without_padding = []

        for group_index, group in enumerate(groups_with_padding):
            group_paddings = self._get_local_group_paddings(group_index)

            lean_sub_partitions = []
            for sub_partition, padding in zip(group, group_paddings):
                lean_length = sub_partition.numel() - padding
                lean_sub_partitions.append(sub_partition[:lean_length])
            groups_without_padding.append(lean_sub_partitions)

        return groups_without_padding

    # Return optimizer state after removing paddings that are added for alignment.
    def _get_state_without_padding(self, state_with_padding, padding):
        lean_state = {}
        for key, value in state_with_padding.items():
            if torch.is_tensor(value):
                lean_length = value.numel() - padding
                lean_state[key] = value[:lean_length]
            else:
                lean_state[key] = value

        return lean_state

    # Return base optimizer states.
    # This method assumes that each param group contains a single flattened tensor.
    def _get_base_optimizer_state(self):
        optimizer_groups_state = []

        for group_index, group in enumerate(self.optimizer.param_groups):
            param_paddings = self._get_local_group_paddings(group_index)

            group_lean_state = []
            for param_idx, param in enumerate(group['params']):
                lean_state = self._get_state_without_padding(self.optimizer.state[param],
                                                             param_paddings[param_idx])
                group_lean_state.append(lean_state)

            optimizer_groups_state.append(group_lean_state)

        return optimizer_groups_state

    def _rigid_state_dict(self):
        """
            Returns a dict that can be loaded for continued training with same DP degree
        """
        """
        Returns a dict containing the current state of this :class:`FP16_Optimizer` instance.
        This dict contains attributes of :class:`FP16_Optimizer`, as well as the state_dict
        of the contained Pytorch optimizer.
        Example::
            checkpoint = {}
            checkpoint['model'] = model.state_dict()
            checkpoint['optimizer'] = optimizer.state_dict()
            torch.save(checkpoint, "saved.pth")
        """
        state_dict = {}
        state_dict['loss_scaler'] = self.loss_scaler
        state_dict['dynamic_loss_scale'] = self.dynamic_loss_scale
        state_dict['overflow'] = self.overflow
        state_dict['base_optimizer_state'] = self.optimizer.state_dict()
        state_dict[
            'local_sub_partitions_of_fp32_groups'] = self.local_sub_partitions_of_fp32_groups
        return state_dict

    def _elastic_state_dict(self):
        """
            Returns a dict that can be loaded for elastic training with different DP degree
        """
        state_dict = {}
        state_dict['loss_scaler'] = self.loss_scaler
        state_dict['dynamic_loss_scale'] = self.dynamic_loss_scale
        state_dict['overflow'] = self.overflow
        state_dict['base_optimizer_state'] = self._get_base_optimizer_state()

        state_dict['zero_stage'] = ZERO_OPTIMIZATION_OPTIMIZER_STATES
        state_dict['partition_count'] = self.partition_count
        state_dict['num_comm_intervals_per_group'] = self.num_comm_intervals_per_group

        # Remove paddings for DP alignment to enable loading for other alignment values
        fp32_groups_without_padding = self._get_groups_without_padding(
            self.local_sub_partitions_of_fp32_groups)
        state_dict['local_sub_partitions_of_fp32_groups'] = fp32_groups_without_padding

        return state_dict

    def state_dict(self):
        """
        Returns a dict containing the current state of this :class:`FP16_Optimizer` instance.
        This dict contains attributes of :class:`FP16_Optimizer`, as well as the state_dict
        of the contained Pytorch optimizer.
        Example::
            checkpoint = {}
            checkpoint['model'] = model.state_dict()
            checkpoint['optimizer'] = optimizer.state_dict()
            torch.save(checkpoint, "saved.pth")
        """
        if self.elastic_checkpoint:
            return self._elastic_state_dict()

        return self._rigid_state_dict()

    # Extract the fp32 weights of the current rank from checkpoint by merging the
    # sub partitions of communication intervals across ranks.
    # Let sub_i_j = sub partition of rank i and comm interval j
    # For 2 ranks and 2 comm intervals, checkpoints (minus padding) are as follows:
    # rank 0 = [sub_0_0, sub_0_1]
    # rank 1 = [sub_1_0, sub_1_1]
    # Merge to get [sub_0_0, sub_1_0, sub_0_1, sub_1_1] => original un-padded flattened tensor.
    def _retrieve_group_sub_partition_weights(self,
                                              all_partition_fp32_weights,
                                              max_elems_per_comm):
        num_partitions = len(all_partition_fp32_weights)
        num_comm_intervals = len(all_partition_fp32_weights[0])
        num_sub_partitions = num_partitions * num_comm_intervals
        all_sub_partition_weights = [None] * num_sub_partitions

        for rank, partition_weights in enumerate(all_partition_fp32_weights):
            for comm_idx, sub_partition_weights in enumerate(partition_weights):
                #all_sub_partition_weights.append(sub_partition_weights)
                sub_partition_idx = (comm_idx * num_partitions) + rank
                all_sub_partition_weights[sub_partition_idx] = sub_partition_weights

        flat_merged_weights = flatten_dense_tensors_sub_partition_aligned(
            tensor_list=all_sub_partition_weights,
            dp=dist.get_world_size(group=self.dp_process_group),
            max_elements_per_comm=max_elems_per_comm,
            pg=self.dp_process_group)

        comm_partitions, dp_sub_partitions, element_intervals, sub_partition_size, num_comm_intervals = \
            self.get_data_parallel_sub_partitions(
                tensor=flat_merged_weights,
                max_elements_per_comm=max_elems_per_comm,
                world_size=dist.get_world_size(group=self.dp_process_group),
                dp_process_group=self.dp_process_group
            )

        partition_id = dist.get_rank(group=self.dp_process_group)
        return [sub_partition for sub_partition in dp_sub_partitions[partition_id]]

    # Restore base optimizer fp32 weights from checkpoint by:
    # 1) Merging fp32 weights from checkpoints of all partitions
    # 2) Extracting fp32 weights for current partition from merged weights
    # 3) Using extracted weights to update base optimizer weights directly.
    def _restore_from_fp32_weights(self, all_state_dict):
        sub_partition_of_fp32_groups = []
        for group_idx in range(len(self.local_sub_partitions_of_fp32_groups)):
            all_partition_fp32_weights = [
                sd['local_sub_partitions_of_fp32_groups'][group_idx]
                for sd in all_state_dict
            ]
            max_elems_per_comm = self.max_elems_per_comm[group_idx]

            sub_partition_weights = self._retrieve_group_sub_partition_weights(
                all_partition_fp32_weights,
                max_elems_per_comm)
            sub_partition_of_fp32_groups.append(sub_partition_weights)

        for current_group, saved_group in zip(self.local_sub_partitions_of_fp32_groups, sub_partition_of_fp32_groups):
            for current_sub_part, saved_sub_part in zip(current_group, saved_group):
                current_sub_part.data.copy_(saved_sub_part.data)

    # Extract optimizer state for current partition from merged states of all partitions
    def _partition_base_optimizer_state(self,
                                        state_key,
                                        all_partition_states,
                                        max_elems_per_comm):
        if not torch.is_tensor(all_partition_states[0]):
            return all_partition_states[0]

        alignment = dist.get_world_size(group=self.dp_process_group)
        flat_merged_partitions = flatten_dense_tensors_sub_partition_aligned(
            tensor_list=all_partition_states,
            dp=dist.get_world_size(group=self.dp_process_group),
            max_elements_per_comm=max_elems_per_comm,
            pg=self.dp_process_group)

        comm_partitions, dp_sub_partitions, element_intervals, sub_partition_size, num_comm_intervals = \
            self.get_data_parallel_sub_partitions(
                tensor=flat_merged_partitions,
                max_elements_per_comm=max_elems_per_comm,
                world_size=dist.get_world_size(group=self.dp_process_group),
                dp_process_group=self.dp_process_group
            )

        partition_id = dist.get_rank(group=self.dp_process_group)
        return [sub_partition for sub_partition in dp_sub_partitions[partition_id]]

    # Compute the optimizer state partitions for the group by
    # 1) Merging state values across the previous partitioning.
    # 2) Repartition state values for the new partitioning
    # 3) Return state corresponding to local partition
    def _retrieve_group_optimizer_states(self, all_partition_states, max_elems_per_comm):
        merged_optimizer_states = {}
        num_partitions = len(all_partition_states)
        num_comm_intervals = len(all_partition_states[0])
        num_sub_partitions = num_partitions * num_comm_intervals

        for rank, partition_state in enumerate(all_partition_states):
            for comm_idx, sub_partition_state in enumerate(partition_state):
                for key, value in sub_partition_state.items():
                    if not key in merged_optimizer_states.keys():
                        merged_optimizer_states[key] = [None] * num_sub_partitions

                    sub_partition_idx = (comm_idx * num_partitions) + rank
                    merged_optimizer_states[key][sub_partition_idx] = value

        group_optimizer_states = {}
        for key, value in merged_optimizer_states.items():
            group_optimizer_states[key] = self._partition_base_optimizer_state(
                key,
                value,
                max_elems_per_comm)

        return group_optimizer_states

    # Restore base optimizer state from checkpoint by
    # 1) Merging optimizer state from checkpoints of all partitions
    # 2) Extracting optimizer state for current partition from the merged state
    # 3) Using the extracted value to directly update the base optimizer.
    def _restore_base_optimizer_state(self, state_dict_list):
        base_optimizer_group_states = []
        for group_idx in range(len(self.optimizer.param_groups)):
            all_partition_group_states = [
                sd['base_optimizer_state'][group_idx] for sd in state_dict_list
            ]
            max_elems_per_comm = self.max_elems_per_comm[group_idx]
            group_optimizer_states = self._retrieve_group_optimizer_states(
                all_partition_group_states,
                max_elems_per_comm)
            base_optimizer_group_states.append(group_optimizer_states)

        for group_idx, group in enumerate(self.optimizer.param_groups):
            for param_idx, param in enumerate(group['params']):
                for key, saved in base_optimizer_group_states[group_idx].items():
                    if torch.is_tensor(self.optimizer.state[param][key]):
                        current = self.optimizer.state[param][key]
                        current.data.copy_(saved[param_idx].data)
                    else:
                        self.optimizer.state[param][key] = saved

    # Restore base optimizer fp32 weights from ZeRO fp16 weights
    def _restore_from_fp16_weights(self):
        partition_id = dist.get_rank(group=self.dp_process_group)
        for fp16_partitions, fp32_partitions in zip(self.parallel_sub_partitioned_fp16_groups, self.local_sub_partitions_of_fp32_groups):
            for fp16_sub_partition, fp32_sub_partition in zip(fp16_partitions[partition_id], fp32_partitions):
                fp32_sub_partition.data.copy_(fp16_sub_partition.data)

    # Refresh the fp32 master params from the fp16 copies.
    def refresh_fp32_params(self):
        self._restore_from_fp16_weights()

    def _rigid_load_state_dict(self, state_dict, load_optimizer_states=True):

        # I think it should actually be ok to reload the optimizer before the model.
        self.loss_scaler = state_dict['loss_scaler']
        self.dynamic_loss_scale = state_dict['dynamic_loss_scale']
        self.overflow = state_dict['overflow']
        if load_optimizer_states:
            self.optimizer.load_state_dict(state_dict['base_optimizer_state'])

        for curr_group, saved_group in zip(self.local_sub_partitions_of_fp32_groups, state_dict['local_sub_partitions_of_fp32_groups']):
            for curr_param, saved_param in zip(curr_group, saved_group):
                curr_param.data.copy_(saved_param.data)

    def _elastic_load_state_dict(self,
                                 state_dict_list,
                                 load_optimizer_states=True,
                                 load_from_fp32_weights=False):
        """
        Loads a state_dict created by an earlier call to state_dict().
        If ``fp16_optimizer_instance`` was constructed from some ``init_optimizer``,
        whose parameters in turn came from ``model``, it is expected that the user
        will call ``model.load_state_dict()`` before
        ``fp16_optimizer_instance.load_state_dict()`` is called.
        Example::
            model = torch.nn.Linear(D_in, D_out).cuda().half()
            optimizer = torch.optim.SGD(model.parameters(), lr=1e-3)
            optimizer = FP16_Optimizer(optimizer, static_loss_scale = 128.0)
            ...
            checkpoint = torch.load("saved.pth")
            model.load_state_dict(checkpoint['model'])
            optimizer.load_state_dict(checkpoint['optimizer'])
        """
        # I think it should actually be ok to reload the optimizer before the model.
        self.loss_scaler = state_dict_list[0]['loss_scaler']
        self.dynamic_loss_scale = state_dict_list[0]['dynamic_loss_scale']
        self.overflow = state_dict_list[0]['overflow']

        if load_optimizer_states:
            self._restore_base_optimizer_state(state_dict_list)

        if load_from_fp32_weights:
            self._restore_from_fp32_weights(state_dict_list)
        else:
            self._restore_from_fp16_weights()

    def load_state_dict(self,
                        state_dict_list,
                        load_optimizer_states=True,
                        load_from_fp32_weights=False):
        """
        Loads a state_dict created by an earlier call to state_dict().
        If ``fp16_optimizer_instance`` was constructed from some ``init_optimizer``,
        whose parameters in turn came from ``model``, it is expected that the user
        will call ``model.load_state_dict()`` before
        ``fp16_optimizer_instance.load_state_dict()`` is called.
        Example::
            model = torch.nn.Linear(D_in, D_out).cuda().half()
            optimizer = torch.optim.SGD(model.parameters(), lr=1e-3)
            optimizer = FP16_Optimizer(optimizer, static_loss_scale = 128.0)
            ...
            checkpoint = torch.load("saved.pth")
            model.load_state_dict(checkpoint['model'])
            optimizer.load_state_dict(checkpoint['optimizer'])
        """
        if self.elastic_checkpoint:
            self._elastic_load_state_dict(state_dict_list,
                                          load_optimizer_states,
                                          load_from_fp32_weights)
        else:
            self._rigid_load_state_dict(
                state_dict_list[dist.get_rank(group=self.dp_process_group)],
                load_optimizer_states)

    def _dump_optimizer_state(self, message):
        logger.info(f'{message}')
        for i, group in enumerate(self.optimizer.param_groups):
            for j, param in enumerate(group['params']):
                for key, value in self.optimizer.state[param].items():
                    t_stats = [
                        value.min(),
                        value.max(),
                        (value.max() - value.min()),
                        value.mean()
                    ]
                    stats = [float(t) for t in t_stats]
                    logger.info(
                        f'group/param/key/min/max/delta/mean = {i}, {j}, {key}: {stats}')
