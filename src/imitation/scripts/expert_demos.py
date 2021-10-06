"""Uses RL to train an expert policy from scratch, saving rollouts and policy."""

import logging
import os
import os.path as osp
from typing import Any, Mapping, Optional

import sacred.run
from sacred.observers import FileStorageObserver
from stable_baselines3.common import callbacks
from stable_baselines3.common.vec_env import VecNormalize

import imitation.util.sacred as sacred_util
from imitation.data import rollout, wrappers
from imitation.policies import serialize
from imitation.rewards.reward_wrapper import RewardVecEnvWrapper
from imitation.rewards.serialize import load_reward
from imitation.scripts.config.expert_demos import expert_demos_ex
from imitation.util import logger, util, video_wrapper, wb_logger


@expert_demos_ex.main
def rollouts_and_policy(
    *,
    _run: sacred.run.Run,
    _seed: int,
    _config: Mapping[str, Any],
    env_name: str,
    env_make_kwargs: Optional[Mapping[str, Any]],
    num_vec: int,
    parallel: bool,
    total_timesteps: int,
    max_episode_steps: Optional[int],
    normalize: bool,
    normalize_kwargs: dict,
    init_rl_kwargs: dict,
    n_episodes_eval: int,
    reward_type: Optional[str],
    reward_path: Optional[str],
    rollout_save_final: bool,
    rollout_save_n_timesteps: Optional[int],
    rollout_save_n_episodes: Optional[int],
    policy_save_interval: int,
    policy_save_final: bool,
    log_dir: str,
    wb_integration: bool,
    video_tracking: bool,
    video_save_interval: int,
) -> Mapping[str, float]:
    """Trains an expert policy from scratch and saves the rollouts and policy.

    Checkpoints:
      At applicable training steps `step` (where step is either an integer or
      "final"):

        - Policies are saved to `{log_dir}/policies/{step}/`.
        - Rollouts are saved to `{log_dir}/rollouts/{step}.pkl`.

    Args:
        env_name: The gym.Env name. Loaded as VecEnv.
        env_make_kwargs: The kwargs passed to `spec.make` of a gym environment.
        num_vec: Number of environments in VecEnv.
        parallel: If True, then use DummyVecEnv. Otherwise use SubprocVecEnv.
        total_timesteps: Number of training timesteps in `model.learn()`.
        max_episode_steps: If not None, then environments are wrapped by
            TimeLimit so that they have at most `max_episode_steps` steps per
            episode.
        normalize: If True, then rescale observations and reward.
        normalize_kwargs: kwargs for `VecNormalize`.
        init_rl_kwargs: kwargs for `init_rl`.
        n_episodes_eval: The number of episodes to average over when calculating
            the average ground truth reward return of the final policy.
        reward_type: If provided, then load the serialized reward of this type,
            wrapping the environment in this reward. This is useful to test
            whether a reward model transfers. For more information, see
            `imitation.rewards.serialize.load_reward`.
        reward_path: A specifier, such as a path to a file on disk, used by
            reward_type to load the reward model. For more information, see
            `imitation.rewards.serialize.load_reward`.
        rollout_save_final: If True, then save rollouts right after training is
            finished.
        rollout_save_n_timesteps: The minimum number of timesteps saved in every
            file. Could be more than `rollout_save_n_timesteps` because
            trajectories are saved by episode rather than by transition.
            Must set exactly one of `rollout_save_n_timesteps`
            and `rollout_save_n_episodes`.
        rollout_save_n_episodes: The number of episodes saved in every
            file. Must set exactly one of `rollout_save_n_timesteps` and
            `rollout_save_n_episodes`.
        policy_save_interval: The number of training updates between in between
            intermediate rollout saves. If the argument is nonpositive, then
            don't save intermediate updates.
        policy_save_final: If True, then save the policy right after training is
            finished.
        log_dir: The root directory to save metrics and checkpoints to.
        wb_integration: If True, then use wandb to log metrics.
        video_tracking: If True, then use video_wrappers to log videos.
        video_save_interval: The number of episodes between saving videos.

    Returns:
        The return value of `rollout_stats()` using the final policy.
    """
    os.makedirs(log_dir, exist_ok=True)
    sacred_util.build_sacred_symlink(log_dir, _run)

    sample_until = rollout.make_sample_until(
        rollout_save_n_timesteps,
        rollout_save_n_episodes,
    )
    eval_sample_until = rollout.make_min_episodes(n_episodes_eval)

    logging.basicConfig(level=logging.INFO)

    custom_writers = []
    if wb_integration:
        writer = wb_logger.WandbOutputFormat(
            wb_options=_config["wb_options"],
            config=_config,
        )
        custom_writers.append(writer)
    custom_logger = logger.configure(
        folder=osp.join(log_dir, "rl"),
        format_strs=["tensorboard", "stdout"],
        custom_writers=custom_writers,
    )

    rollout_dir = osp.join(log_dir, "rollouts")
    policy_dir = osp.join(log_dir, "policies")
    os.makedirs(rollout_dir, exist_ok=True)
    os.makedirs(policy_dir, exist_ok=True)

    post_wrappers = [lambda env, idx: wrappers.RolloutInfoWrapper(env)]
    if video_tracking:
        # Only wrap the first environment for video tracking
        video_writing_dir = osp.join(log_dir, "videos")
        post_wrappers += [
            lambda env, idx: video_wrapper.VideoWrapper(
                env=env,
                directory=video_writing_dir,
                single_video=False,
                save_interval=video_save_interval,
            )
            if idx == 0
            else env,
        ]

    venv = util.make_vec_env(
        env_name,
        num_vec,
        seed=_seed,
        parallel=parallel,
        log_dir=log_dir,
        max_episode_steps=max_episode_steps,
        post_wrappers=post_wrappers,
        env_make_kwargs=env_make_kwargs,
    )

    callback_objs = []
    if reward_type is not None:
        reward_fn = load_reward(reward_type, reward_path, venv)
        venv = RewardVecEnvWrapper(venv, reward_fn)
        callback_objs.append(venv.make_log_callback())
        logging.info(f"Wrapped env in reward {reward_type} from {reward_path}.")

    vec_normalize = None
    if normalize:
        venv = vec_normalize = VecNormalize(venv, **normalize_kwargs)

    if policy_save_interval > 0:
        save_policy_callback = serialize.SavePolicyCallback(policy_dir, vec_normalize)
        save_policy_callback = callbacks.EveryNTimesteps(
            policy_save_interval,
            save_policy_callback,
        )
        callback_objs.append(save_policy_callback)
    callback = callbacks.CallbackList(callback_objs)

    policy = util.init_rl(venv, verbose=1, **init_rl_kwargs)
    policy.set_logger(custom_logger)
    policy.learn(total_timesteps, callback=callback)

    # Save final artifacts after training is complete.
    if rollout_save_final:
        save_path = osp.join(rollout_dir, "final.pkl")
        rollout.rollout_and_save(save_path, policy, venv, sample_until)
    if policy_save_final:
        output_dir = os.path.join(policy_dir, "final")
        serialize.save_stable_model(output_dir, policy, vec_normalize)

    # Final evaluation of expert policy.
    trajs = rollout.generate_trajectories(policy, venv, eval_sample_until)
    stats = rollout.rollout_stats(trajs)

    return stats


@expert_demos_ex.command
def rollouts_from_policy(
    *,
    _run: sacred.run.Run,
    _seed: int,
    env_name: str,
    env_make_kwargs: Optional[Mapping[str, Any]],
    num_vec: int,
    parallel: bool,
    max_episode_steps: Optional[int],
    rollout_save_n_timesteps: int,
    rollout_save_n_episodes: int,
    policy_path: str,
    policy_type: str,
    rollout_save_path: str,
    log_dir: str,
) -> None:
    """Loads a saved policy and generates rollouts.

    Unlisted arguments are the same as in `rollouts_and_policy()`.

    Args:
        env_name: The gym.Env name. Loaded as VecEnv.
        env_make_kwargs: The kwargs passed to `spec.make` of a gym environment.
        num_vec: Number of environments in VecEnv.
        parallel: If True, then use DummyVecEnv. Otherwise use SubprocVecEnv.
        max_episode_steps: If not None, then environments are wrapped by
            TimeLimit so that they have at most `max_episode_steps` steps per
            episode.
        rollout_save_n_timesteps: The minimum number of timesteps saved in every
            file. Could be more than `rollout_save_n_timesteps` because
            trajectories are saved by episode rather than by transition.
            Must set exactly one of `rollout_save_n_timesteps`
            and `rollout_save_n_episodes`.
        rollout_save_n_episodes: The number of episodes saved in every
            file. Must set exactly one of `rollout_save_n_timesteps` and
            `rollout_save_n_episodes`.
        policy_type: Argument to `imitation.policies.serialize.load_policy`.
        policy_path: Argument to `imitation.policies.serialize.load_policy`.
        rollout_save_path: Rollout pickle is saved to this path.
        log_dir: The root directory to save metrics and checkpoints to.
    """
    os.makedirs(log_dir, exist_ok=True)
    sacred_util.build_sacred_symlink(log_dir, _run)

    sample_until = rollout.make_sample_until(
        rollout_save_n_timesteps,
        rollout_save_n_episodes,
    )

    venv = util.make_vec_env(
        env_name,
        num_vec,
        seed=_seed,
        parallel=parallel,
        log_dir=log_dir,
        max_episode_steps=max_episode_steps,
        post_wrappers=[lambda env, idx: wrappers.RolloutInfoWrapper(env)],
        env_make_kwargs=env_make_kwargs,
    )

    policy = serialize.load_policy(policy_type, policy_path, venv)
    rollout.rollout_and_save(rollout_save_path, policy, venv, sample_until)


def main_console():
    observer = FileStorageObserver(osp.join("output", "sacred", "expert_demos"))
    expert_demos_ex.observers.append(observer)
    expert_demos_ex.run_commandline()


if __name__ == "__main__":  # pragma: no cover
    main_console()
