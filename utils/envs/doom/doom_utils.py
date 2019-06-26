from gym.spaces import Discrete

from utils.envs.doom.doom_gym import VizdoomEnv
from utils.envs.doom.multiplayer.doom_multiagent import VizdoomEnvMultiplayer, VizdoomMultiAgentEnv
from utils.envs.doom.wrappers.action_space import doom_action_space
from utils.envs.doom.wrappers.additional_input import DoomAdditionalInput
from utils.envs.doom.wrappers.observation_space import SetResolutionWrapper
from utils.envs.doom.wrappers.step_human_input import StepHumanInput
from utils.envs.env_wrappers import ResizeWrapper, RewardScalingWrapper, TimeLimitWrapper

DOOM_W = 128
DOOM_H = 72


class DoomCfg:
    def __init__(self, name, env_cfg, action_space, reward_scaling, default_timeout, no_idle=False):
        self.name = name
        self.env_cfg = env_cfg
        self.action_space = action_space
        self.reward_scaling = reward_scaling
        self.default_timeout = default_timeout

        # set to True if the environment does not assume an IDLE action
        self.no_idle = no_idle


DOOM_ENVS = [
    DoomCfg('doom_basic', 'basic.cfg', Discrete(3), 0.01, 300, no_idle=True),

    DoomCfg('doom_battle', 'D3_battle.cfg', Discrete(9), 1.0, 2100),
    DoomCfg('doom_battle_tuple_actions', 'D3_battle.cfg', doom_action_space(), 1.0, 2100),

    DoomCfg('doom_battle2', 'D4_battle2.cfg', Discrete(9), 1.0, 2100),

    DoomCfg('doom_dm', 'cig.cfg', doom_action_space(), 1.0, int(1e9)),
    DoomCfg('doom_dm_test', 'cig.cfg', doom_action_space(), 1.0, int(1e9)),
]


def doom_env_by_name(name):
    for cfg in DOOM_ENVS:
        if cfg.name == name:
            return cfg
    raise Exception('Unknown Doom env')


# noinspection PyUnusedLocal
def make_doom_env(
        doom_cfg, mode='train',
        skip_frames=True, human_input=False,
        show_automap=False, episode_horizon=None,
        player_id=None, num_players=None,  # for multi-agent
        env_config=None,
        **kwargs,
):
    skip_frames = 4 if skip_frames else 1

    if player_id is None:
        env = VizdoomEnv(doom_cfg.action_space, doom_cfg.env_cfg, skip_frames=skip_frames)
    else:
        env = VizdoomEnvMultiplayer(
            doom_cfg.action_space, doom_cfg.env_cfg,
            player_id=player_id, num_players=num_players, skip_frames=skip_frames,
        )

    env.no_idle_action = doom_cfg.no_idle

    if human_input:
        env = StepHumanInput(env)

    # courtesy of https://github.com/pathak22/noreward-rl/blob/master/src/envs.py
    # and https://github.com/ppaquette/gym-doom
    if mode == 'test':
        env = SetResolutionWrapper(env, '800x450')
    else:
        env = SetResolutionWrapper(env, '256x144')

    h, w, channels = env.observation_space.shape
    if w != DOOM_W:
        env = ResizeWrapper(env, DOOM_W, DOOM_H, grayscale=False)

    # randomly vary episode duration to somewhat decorrelate the experience
    timeout = doom_cfg.default_timeout - 50
    if episode_horizon is not None and episode_horizon > 0:
        timeout = episode_horizon
    env = TimeLimitWrapper(env, limit=timeout, random_variation_steps=49)

    if doom_cfg.reward_scaling != 1.0:
        env = RewardScalingWrapper(env, doom_cfg.reward_scaling)

    env = DoomAdditionalInput(env)
    return env


def make_doom_multiagent_env(
        doom_cfg, mode='train',
        skip_frames=True, num_players=None, env_config=None,
        **kwargs,
):
    def make_env_func(player_id, num_players_):
        return make_doom_env(
            doom_cfg, mode,
            skip_frames=skip_frames,
            player_id=player_id, num_players=num_players_,
            **kwargs,
        )

    env = VizdoomMultiAgentEnv(
        num_players=num_players,
        make_env_func=make_env_func,
        env_config=env_config,
        skip_frames=skip_frames,
    )
    return env
