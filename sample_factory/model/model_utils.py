from collections import namedtuple
from typing import List, Optional

import torch
from torch import nn
from torch.nn.utils import spectral_norm

from sample_factory.cfg.configurable import Configurable
from sample_factory.utils.typing import Config


def get_rnn_size(cfg):
    """Original implementation to get RNN latent dimension"""
    if cfg.use_rnn:
        size = cfg.rnn_size * cfg.rnn_num_layers
    else:
        size = 1

    if cfg.rnn_type == "lstm":
        size *= 2

    if not cfg.actor_critic_share_weights:
        # actor and critic need separate states
        size *= 2

    return size


# NOTE: names "RNNSpace" and namedtuple needs to be the same for pickle to work
RNNSpace = namedtuple("RNNSpace", ["dtype", "shape"]) 
def get_rnn_info(cfg):
    """Get RNN intermediate states as a dict of items"""
    spaces = dict()
    if cfg.use_rnn:
        # TODO ant 2023-11-19: is this the best way to initialize from cfg?
        deter_shape = (cfg.rnn_determinstic_size,)
        stoch_shape = (cfg.rnn_stochastic_size, cfg.rnn_discrete_size)
        
        spaces["deter"] = RNNSpace(dtype="float32", shape=deter_shape)
        spaces["stoch"] = RNNSpace(dtype="float32", shape=stoch_shape)
        spaces["logit"] = RNNSpace(dtype="float32", shape=stoch_shape)
    else:
        spaces["deter"] = RNNSpace(dtype="float32", shape=(1,))

    if cfg.rnn_type == "lstm":
        raise NotImplementedError  # TODO 2023-12-15; need to check this work 
        #import pdb; pdb.set_trace()
        #for k in spaces:
        #    spaces[k].shape   double the space?    

    if not cfg.actor_critic_share_weights:
        # actor and critic need separate states
        size *= 2
        raise NotImplementedError

    return spaces


def get_goal_size(cfg):
    """AC: get size of goal from cfg"""
    goal_size = cfg.goal_size
    return goal_size


def nonlinearity(cfg: Config, inplace: bool = False) -> nn.Module:
    if cfg.nonlinearity == "elu":
        return nn.ELU(inplace=inplace)
    elif cfg.nonlinearity == "relu":
        return nn.ReLU(inplace=inplace)
    elif cfg.nonlinearity == "tanh":
        return nn.Tanh()
    else:
        raise Exception(f"Unknown {cfg.nonlinearity=}")


def fc_layer(in_features: int, out_features: int, bias=True, spec_norm=False) -> nn.Module:
    layer = nn.Linear(in_features, out_features, bias)
    if spec_norm:
        layer = spectral_norm(layer)

    return layer


def create_mlp(layer_sizes: List[int], input_size: int, activation: nn.Module) -> nn.Module:
    """Sequential fully connected layers."""
    layers = []
    for i, size in enumerate(layer_sizes):
        layers.extend([fc_layer(input_size, size), activation])
        input_size = size

    if len(layers) > 0:
        return nn.Sequential(*layers)
    else:
        return nn.Identity()


class ModelModule(nn.Module, Configurable):
    def __init__(self, cfg: Config):
        nn.Module.__init__(self)
        Configurable.__init__(self, cfg)

    def get_out_size(self):
        raise NotImplementedError()


def model_device(model: nn.Module) -> Optional[torch.device]:
    try:
        return next(model.parameters()).device
    except StopIteration:
        return None
