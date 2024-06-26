import random
from typing import Dict, Iterable, List, Optional, Tuple

import torch
from torch.utils.data import DataLoader
from signal_slot.signal_slot import EventLoop, signal

from sample_factory.algo.learning.dataset_utils import BatcherRamDataset
from sample_factory.algo.utils.env_info import EnvInfo
from sample_factory.algo.utils.heartbeat import HeartbeatStoppableEventLoopObject
from sample_factory.algo.utils.shared_buffers import BufferMgr, alloc_trajectory_tensors, policy_device
from sample_factory.algo.utils.tensor_dict import TensorDict, clone_tensordict
from sample_factory.model.model_utils import get_rnn_size, get_rnn_info, get_goal_size
from sample_factory.utils.attr_dict import AttrDict
from sample_factory.utils.timing import Timing
from sample_factory.utils.typing import Device, PolicyID
from sample_factory.utils.utils import debug_log_every_n, log


def slice_len(s: slice) -> int:
    return s.stop - s.start


class SliceMerger:
    def __init__(self):
        self.slice_starts: Dict[int, slice] = dict()
        self.slice_stops: Dict[int, slice] = dict()
        self.total_num = 0

    def _add_slice(self, s):
        self.slice_starts[s.start] = s
        self.slice_stops[s.stop] = s
        self.total_num += slice_len(s)

    def _del_slice(self, s: slice):
        del self.slice_starts[s.start]
        del self.slice_stops[s.stop]
        self.total_num -= slice_len(s)

    def merge_slices(self, trajectory_slice: slice):
        new_slice = None

        if prev_slice := self.slice_stops.get(trajectory_slice.start):
            # merge with a slice that preceeds ours in the buffer
            new_slice = slice(prev_slice.start, trajectory_slice.stop)
            # delete the previous slice from both maps
            self._del_slice(prev_slice)
        elif next_slice := self.slice_starts.get(trajectory_slice.stop):
            # merge with a slice that is next in the buffer
            new_slice = slice(trajectory_slice.start, next_slice.stop)
            self._del_slice(next_slice)

        if new_slice:
            # successfully merged some slices, keep going
            self.merge_slices(new_slice)
        else:
            # nothing to merge, just add a new slice
            self._add_slice(trajectory_slice)

    def _extract_at_most(self, s: slice, batch_size: int) -> slice:
        n = slice_len(s)
        self._del_slice(s)
        if n > batch_size:
            remaining_slice = slice(s.start + batch_size, s.stop)
            self._add_slice(remaining_slice)
            s = slice(s.start, s.start + batch_size)

        return s

    def get_at_most(self, batch_size) -> Optional[slice]:
        for s in self.slice_starts.values():
            return self._extract_at_most(s, batch_size)

        return None

    def get_exactly(self, batch_size: int) -> Optional[slice]:
        """
        At this point, all trajectory slices that share a boundary should have been merged into longer slices.
        If there's a slice that is at least trajectories_per_batch long starting where the previous returned slice
        ends - we found our batch.
        :return: a slice of trajectory buffer that will be a training batch on the learner
        """
        for slice_start, s in self.slice_starts.items():
            n = slice_len(s)
            if n >= batch_size:
                return self._extract_at_most(s, batch_size)

        return None


class Batcher(HeartbeatStoppableEventLoopObject):
    def __init__(
        self, evt_loop: EventLoop, policy_id: PolicyID, buffer_mgr: BufferMgr, cfg: AttrDict, env_info: EnvInfo
    ):
        unique_name = f"{Batcher.__name__}_{policy_id}"
        super().__init__(evt_loop, unique_name, cfg.heartbeat_interval)

        self.timing = Timing(name=f"Batcher {policy_id} profile")

        self.cfg = cfg
        self.env_info: EnvInfo = env_info
        self.policy_id = policy_id

        self.training_iteration: int = 0

        self.traj_per_training_iteration = buffer_mgr.trajectories_per_training_iteration
        self.traj_per_sampling_iteration = buffer_mgr.sampling_trajectories_per_iteration

        self.slices_for_training: Dict[Device, SliceMerger] = {
            device: SliceMerger() for device in buffer_mgr.traj_tensors_torch
        }
        self.slices_for_sampling: Dict[Device, SliceMerger] = {
            device: SliceMerger() for device in buffer_mgr.traj_tensors_torch
        }

        self.traj_buffer_queues = buffer_mgr.traj_buffer_queues
        self.traj_tensors = buffer_mgr.traj_tensors_torch
        self.training_batches: List[TensorDict] = []

        self.max_batches_to_accumulate = buffer_mgr.max_batches_to_accumulate
        self.available_batches = list(range(self.max_batches_to_accumulate))
        self.traj_tensors_to_release: List[List[Tuple[Device, slice]]] = [
            [] for _ in range(self.max_batches_to_accumulate)
        ]

    @signal
    def initialized(self):
        ...

    @signal
    def trajectory_buffers_available(self):
        ...

    @signal
    def training_batches_available(self):
        ...

    @signal
    def stop_experience_collection(self):
        ...

    @signal
    def resume_experience_collection(self):
        ...

    @signal
    def stop(self):
        ...

    def init(self):
        device = policy_device(self.cfg, self.policy_id)
        rnn_spaces = get_rnn_info(self.cfg)
        goal_size = get_goal_size(self.cfg)
        for i in range(self.max_batches_to_accumulate):
            training_batch = alloc_trajectory_tensors(
                self.env_info,
                self.traj_per_training_iteration,
                self.cfg.rollout,
                rnn_spaces,
                goal_size,
                device,
                False,
            )
            self.training_batches.append(training_batch)

        self.initialized.emit()

    def on_new_trajectories(self, trajectory_dicts: Iterable[Dict], device: str):
        with self.timing.add_time("batching"):
            for trajectory_dict in trajectory_dicts:
                assert trajectory_dict["policy_id"] == self.policy_id
                trajectory_slice = trajectory_dict["traj_buffer_idx"]
                if not isinstance(trajectory_slice, slice):
                    trajectory_slice = slice(trajectory_slice, trajectory_slice + 1)  # slice of len 1
                # log.debug(f"{self.policy_id} received trajectory slice {trajectory_slice}")
                self.slices_for_training[device].merge_slices(trajectory_slice)

                # NOTE AC 2023-12-16: below added for E3B
                self.traj_tensors['cpu']['env_id'][trajectory_dict['traj_buffer_idx']] = trajectory_dict['unique_env_id']
                self.traj_tensors['cpu']['start_step'][trajectory_dict['traj_buffer_idx']] = trajectory_dict['rollout_step']
                # E3B modification ends

            self._maybe_enqueue_new_training_batches()

    def _maybe_enqueue_new_training_batches(self):
        with torch.no_grad():
            while self.available_batches:
                total_num_trajectories = 0
                for slices in self.slices_for_training.values():
                    total_num_trajectories += slices.total_num

                if total_num_trajectories < self.traj_per_training_iteration:
                    # not enough experience yet to start training
                    break

                # obtain the index of the available batch buffer
                batch_idx = self.available_batches[0]
                self.available_batches.pop(0)
                assert len(self.traj_tensors_to_release[batch_idx]) == 0

                # extract slices of trajectories and copy them to the training batch
                devices = list(self.slices_for_training.keys())
                random.shuffle(devices)  # so that no sampling device is preferred

                trajectories_copied = 0
                remaining = self.traj_per_training_iteration - trajectories_copied
                for device in devices:
                    traj_tensors = self.traj_tensors[device]
                    slices = self.slices_for_training[device]
                    while remaining > 0 and (traj_slice := slices.get_at_most(remaining)):
                        # copy data into the training buffer
                        start = trajectories_copied
                        stop = start + slice_len(traj_slice)

                        # log.debug(f"Copying {traj_slice} trajectories from {device} to {batch_idx}")
                        self.training_batches[batch_idx][start:stop] = traj_tensors[traj_slice]

                        # remember that we need to release these trajectories
                        self.traj_tensors_to_release[batch_idx].append((device, traj_slice))

                        trajectories_copied += slice_len(traj_slice)
                        remaining = self.traj_per_training_iteration - trajectories_copied

                assert trajectories_copied == self.traj_per_training_iteration and remaining == 0

                # signal the learner that we have a new training batch
                self.training_batches_available.emit(batch_idx)

                if self.cfg.async_rl:
                    self._release_traj_tensors(batch_idx)
                    if not self.available_batches:
                        debug_log_every_n(50, "Signal inference workers to stop experience collection...")
                        self.stop_experience_collection.emit()

    def on_training_batch_released(self, batch_idx: int, training_iteration: int):
        with self.timing.add_time("releasing_batches"):
            self.training_iteration = training_iteration

            if not self.cfg.async_rl:
                # in synchronous RL, we release the trajectories after they're processed by the learner
                self._release_traj_tensors(batch_idx)

            if not self.available_batches and self.cfg.async_rl:
                debug_log_every_n(50, "Signal inference workers to resume experience collection...")
                self.resume_experience_collection.emit()

            self.available_batches.append(batch_idx)

            self._maybe_enqueue_new_training_batches()

            # log.debug(
            #     f"{self.object_id} finished processing batch {batch_idx}, available batches: {self.available_batches}, {training_iteration=}"
            # )

    def _release_traj_tensors(self, batch_idx: int):
        new_sampling_batches = dict()

        if self.cfg.batched_sampling:
            for device, traj_slice in self.traj_tensors_to_release[batch_idx]:
                self.slices_for_sampling[device].merge_slices(traj_slice)

            for device, slices in self.slices_for_sampling.items():
                new_sampling_batches[device] = []
                while (sampling_batch := slices.get_exactly(self.traj_per_sampling_iteration)) is not None:
                    new_sampling_batches[device].append(sampling_batch)
        else:
            for device, traj_slice in self.traj_tensors_to_release[batch_idx]:
                if device not in new_sampling_batches:
                    new_sampling_batches[device] = []

                for i in range(traj_slice.start, traj_slice.stop):
                    new_sampling_batches[device].append(i)

            for device in new_sampling_batches:
                new_sampling_batches[device].sort()

        self.traj_tensors_to_release[batch_idx] = []

        for device, batches in new_sampling_batches.items():
            # log.debug(f'Release trajectories {batches}')
            self.traj_buffer_queues[device].put_many(batches)
        self.trajectory_buffers_available.emit(self.policy_id, self.training_iteration)

    def on_stop(self, *args):
        self.stop.emit(self.object_id, {self.object_id: self.timing})
        super().on_stop(*args)


# ==
# Saving buffer using pytorch dataloader
class SavingBatcher(Batcher):
    """
    Batcher that saves in memory all sampled trajectories to sample from

    N.B.: there is a _single_ batcher per policy which handles all of the
          learning data for that policy
    """
    def __init__(
        self, evt_loop: EventLoop, policy_id: PolicyID, buffer_mgr: BufferMgr, cfg: AttrDict, env_info: EnvInfo
    ):
        super().__init__(evt_loop, policy_id, buffer_mgr, cfg, env_info)

        # repeat of some things in super().__init__(...) for clarity
        self.training_batches: List[TensorDict] = [] 
        self.tr_device = None
        self.min_saving_samples = None

        self.max_batches_to_accumulate = cfg.num_batches_to_accumulate
        self.traj_slices_to_release: List[Tuple[Device, slice]] = []

        self._n_slices_merged: int = 0

        # self.available_batches = list(range(self.max_batches_to_accumulate))
        # self.traj_tensors_to_release: List[List[Tuple[Device, slice]]] = [
        #     [] for _ in range(self.max_batches_to_accumulate)
        # ]  # TODO: I think these two are not used?

        self.training_iteration: int = 0
        self._prev_data_collect_iteration: int = 0

        # Dataset items
        self.dataset: BatcherRamDataset = None
        self._dataloader: DataLoader = None
        
    def init(self):
        self.tr_device = policy_device(self.cfg, self.policy_id)
        rnn_spaces = get_rnn_info(self.cfg)
        
        for i in range(self.max_batches_to_accumulate):
            tr_len = self.cfg.rollout if self.cfg.saving_batcher.sample_whole_trajectories \
                else self.cfg.saving_batcher.sample_length
            training_batch = alloc_trajectory_tensors(
                env_info=self.env_info,
                num_traj=self.cfg.saving_batcher.train_n_trajectories,
                rollout=tr_len,
                rnn_spaces=rnn_spaces,
                device=self.tr_device,
                share=False,
            )
            self.training_batches.append(training_batch)

        # TODO: add an assertion statement for this, i.e. only sample exactly
        #       the amount required to fill this dataset, also figure out
        #       the CPU-GPU memory transfer step (have it be on CPU and switch)
        #       to GPU here when sending to learner

        self.min_saving_samples = self.cfg.min_saving_samples

        self.dataset = BatcherRamDataset(
            max_size=self.cfg.saving_batcher.max_size, 
            rollout_length=self.cfg.rollout, 
            env_info=self.env_info, 
            rnn_spaces=rnn_spaces, 
            sample_whole_trajectories=self.cfg.saving_batcher.sample_whole_trajectories, 
            sample_length=self.cfg.saving_batcher.sample_length,
            device="cpu", 
            share=False
        )  # TODO add a seed=???

        # pytorch dataloader 
        # TODO customize this, make better; worker is 0 for serial mode and some number otherwise,
        # other TODO's include pinning memories, etc.
        self._dataloader = DataLoader(
            self.dataset, 
            batch_size=self.cfg.saving_batcher.train_n_trajectories, 
            num_workers=0,
        ) 
        self._data_iter = None

        self.initialized.emit()

    def on_new_trajectories(self, trajectory_dicts: Iterable[Dict], device: str):
        """
        This is called when the sampler has new trajectories; on sampler signal 
        sampler.connect_on_new_trajectories
        """
        with self.timing.add_time("batching"):
            for trajectory_dict in trajectory_dicts:
                assert trajectory_dict["policy_id"] == self.policy_id
                trajectory_slice = trajectory_dict["traj_buffer_idx"]
                if not isinstance(trajectory_slice, slice):
                    trajectory_slice = slice(trajectory_slice, trajectory_slice + 1)  # slice of len 1
                # log.debug(f"{self.policy_id} received trajectory slice {trajectory_slice}")
                self.slices_for_training[device].merge_slices(trajectory_slice)

                self._n_slices_merged += slice_len(trajectory_slice)

            # add to dataset
            self._maybe_enqueue_new_trajectory_data()
            
            # collect a minimum amount of data before starting training
            if self.dataset.num_samples < self.min_saving_samples: 
                log.debug(
                    f"Samples in dataset: {self.dataset.num_samples}; "
                    f"Min samples to start training: {self.min_saving_samples}"
                )
                self._release_traj_tensors()  # signal to do more rollouts
            else:
                self._data_iter = iter(self._dataloader)
                self._maybe_enqueue_new_training_batches()  # train signal

    def _maybe_enqueue_new_trajectory_data(self):
        with torch.no_grad():
            total_num_trajectories = 0
            for slices in self.slices_for_training.values():
                total_num_trajectories += slices.total_num

            # extract slices of trajectories and copy them to the training batch
            devices = list(self.slices_for_training.keys())
            random.shuffle(devices)  # so that no sampling device is preferred

            trajectories_copied = 0 
            remaining = self.traj_per_training_iteration - trajectories_copied   #TODO: do I need all/any of these logic?
            for device in devices:
                traj_tensors = self.traj_tensors[device]
                slices = self.slices_for_training[device]
                while remaining > 0 and (traj_slice := slices.get_at_most(remaining)):
                    self.dataset.add(clone_tensordict(traj_tensors[traj_slice]))
                    # log.debug(f"slice: {traj_slice}; dataset size: {len(self.dataset)}")
                    
                    # remember that we need to release these trajectories
                    self.traj_slices_to_release.append((device, traj_slice))

                    trajectories_copied += slice_len(traj_slice)
                    remaining = self.traj_per_training_iteration - trajectories_copied

    def _maybe_enqueue_new_training_batches(self):
        with torch.no_grad():
            # obtain the index of the available batch buffer
            while self.available_batches:          
                #if total_num_trajectories < self.traj_per_training_iteration:
                    # not enough experience yet to start training 
                    # break

                # obtain the index of the available batch buffer
                batch_idx = self.available_batches[0]
                self.available_batches.pop(0)
                assert len(self.traj_tensors_to_release[batch_idx]) == 0

                try:
                    batch = next(self._data_iter)
                except StopIteration:
                    self._data_iter = iter(self._dataloader)
                    batch = next(self._data_iter)

                self.training_batches[batch_idx] = batch

                # signal the learner that we have a new training batch
                self.training_batches_available.emit(batch_idx)

                if self.cfg.async_rl:
                    raise NotImplementedError("(ANt) Not implemented for saving batcher (yet?)")
            
            pass
       
    def on_training_batch_released(self, batch_idx: int, training_iteration: int):
        """
        This function is triggered by the learner: once the learner is done with
        a batch, it is given back to the batcher
        """
        with self.timing.add_time("releasing_batches"):
            self.training_iteration = training_iteration

            self.available_batches.append(batch_idx)

            # Determining whether to collect more data or not
            # TODO: add a more involved training ratio based on data sampling
            collect_more_data = (
                ((self.training_iteration - self._prev_data_collect_iteration) 
                 >= self.cfg.saving_batcher.train_iter_to_data_collection_ratio) and
                (self.training_iteration > self.cfg.min_initial_train_iters) 
            )

            if collect_more_data:
                self._release_traj_tensors()
                self._prev_data_collect_iteration = self.training_iteration
            
            # Maybe TODO: have this be a "else" statement for when we do not 
            #             do data collection instead?
            self._maybe_enqueue_new_training_batches()

            if self.cfg.async_rl:
                raise NotImplementedError()

    def _release_traj_tensors(self):
        """
        Update self.traj_buffer_queues to indicate which queues are open to 
        store more sampled trajectories; then signal to sampler that
        trajectory buffers are available
        """
        new_sampling_batches = dict()

        if self.cfg.batched_sampling:
            raise NotImplementedError()
        else:
            for device, traj_slice in self.traj_slices_to_release:
                if device not in new_sampling_batches:
                    new_sampling_batches[device] = []

                for i in range(traj_slice.start, traj_slice.stop):
                    new_sampling_batches[device].append(i)

            for device in new_sampling_batches:
                new_sampling_batches[device].sort()

        self.traj_slices_to_release = []

        for device, batches in new_sampling_batches.items():
            # log.debug(f'Release trajectories {batches}')
            self.traj_buffer_queues[device].put_many(batches)
        self.trajectory_buffers_available.emit(self.policy_id, self.training_iteration)
        

    def on_stop(self, *args):
        super().on_stop(*args)
