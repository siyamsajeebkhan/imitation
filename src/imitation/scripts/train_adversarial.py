"""Train GAIL or AIRL.

Can be used as a CLI script, or the `train_and_plot` function can be called directly.
"""

import logging
import os
import os.path as osp
from typing import Any, Mapping, Optional, Type

import torch as th
from sacred.observers import FileStorageObserver
from stable_baselines3.common.vec_env import VecVideoRecorder

from imitation.algorithms.adversarial import airl, gail
from imitation.data import rollout, types
from imitation.policies import serialize
from imitation.rewards import reward_nets
from imitation.scripts.config.train_adversarial import train_adversarial_ex
from imitation.util import logger
from imitation.util import sacred as sacred_util
from imitation.util import util
from imitation.util.logger import WandbOutputFormat


@train_adversarial_ex.capture
def get_wb_options(
    log_dir,
    env_name,
    subtask_str,
    video_tracking,
    postfix,
    wb_tag,
    seed=0,
):
    wb_options = dict(
        name=f"{env_name}-{subtask_str}-seed{seed}{postfix}",
        tags=[str(env_name), subtask_str, f"seed{seed}"] + [wb_tag],
        monitor_gym=True if video_tracking else False,
        save_code=True,
        dir=log_dir,
    )
    return wb_options


def save(trainer, save_path):
    """Save discriminator and generator."""
    # We implement this here and not in Trainer since we do not want to actually
    # serialize the whole Trainer (including e.g. expert demonstrations).
    os.makedirs(save_path, exist_ok=True)
    th.save(trainer.reward_train, os.path.join(save_path, "reward_train.pt"))
    th.save(trainer.reward_test, os.path.join(save_path, "reward_test.pt"))
    # TODO(gleave): unify this with the saving logic in data_collect?
    # (Needs #43 to be merged before attempting.)
    serialize.save_stable_model(
        os.path.join(save_path, "gen_policy"),
        trainer.gen_algo,
        trainer.venv_norm_obs,
    )


@train_adversarial_ex.main
def train_adversarial(
    _run,
    _seed: int,
    _config: Mapping[str, Any],
    algorithm: str,
    env_name: str,
    env_make_kwargs: Optional[Mapping[str, Any]],
    num_vec: int,
    parallel: bool,
    max_episode_steps: Optional[int],
    rollout_path: str,
    n_expert_demos: Optional[int],
    log_dir: str,
    total_timesteps: int,
    n_episodes_eval: int,
    checkpoint_interval: int,
    gen_batch_size: int,
    init_rl_kwargs: Mapping,
    reward_net_cls: Optional[Type[reward_nets.RewardNet]],
    reward_net_kwargs: Optional[Mapping[str, Any]],
    algorithm_kwargs: Mapping[str, Mapping],
    wandb_integration: Optional[bool],
    video_tracking: Optional[bool],
) -> Mapping[str, Mapping[str, float]]:
    """Train an adversarial-network-based imitation learning algorithm.

    Checkpoints:
        - DiscrimNets are saved to `f"{log_dir}/checkpoints/{step}/discrim/"`,
            where step is either the training round or "final".
        - Generator policies are saved to `f"{log_dir}/checkpoints/{step}/gen_policy/"`.

    Args:
        _seed: Random seed.
        algorithm: A case-insensitive string determining which adversarial imitation
            learning algorithm is executed. Either "airl" or "gail".
        env_name: The environment to train in.
        env_make_kwargs: The kwargs passed to `spec.make` of a gym environment.
        num_vec: Number of `gym.Env` to vectorize.
        parallel: Whether to use "true" parallelism. If True, then use `SubProcVecEnv`.
            Otherwise, use `DummyVecEnv` which steps through environments serially.
        max_episode_steps: If not None, then a TimeLimit wrapper is applied to each
            environment to artificially limit the maximum number of timesteps in an
            episode.
        rollout_path: Path to pickle containing list of Trajectories. Used as
            expert demonstrations.
        n_expert_demos: The number of expert trajectories to actually use
            after loading them from `rollout_path`.
            If None, then use all available trajectories.
            If `n_expert_demos` is an `int`, then use exactly `n_expert_demos`
            trajectories, erroring if there aren't enough trajectories. If there are
            surplus trajectories, then use the first `n_expert_demos` trajectories and
            drop the rest.
        log_dir: Directory to save models and other logging to.
        total_timesteps: The number of transitions to sample from the environment
            during training.
        n_episodes_eval: The number of episodes to average over when calculating
            the average episode reward of the imitation policy for return.
        checkpoint_interval: Save the discriminator and generator models every
            `checkpoint_interval` rounds and after training is complete. If 0,
            then only save weights after training is complete. If <0, then don't
            save weights at all.
        gen_batch_size: Batch size for generator updates. Sacred automatically uses
            this to calculate `n_steps` in `init_rl_kwargs`. In the script body, this
            is only used in sanity checks.
        init_rl_kwargs: Keyword arguments for `init_rl`, the RL algorithm initialization
            utility function.
        reward_net_cls: Class of reward network to construct.
        reward_net_kwargs: Keyword arguments passed to reward network constructor.
        algorithm_kwargs: Keyword arguments for the `GAIL` or `AIRL` constructor
            that can apply to either constructor. Unlike a regular kwargs argument, this
            argument can only have the following keys: "shared", "airl", and "gail".
            `algorithm_kwargs["airl"]`, if it is provided, is a kwargs `Mapping` passed
            to the `AIRL` constructor when `algorithm == "airl"`. Likewise
            `algorithm_kwargs["gail"]` is passed to the `GAIL` constructor when
            `algorithm == "gail"`. `algorithm_kwargs["shared"]`, if provided, is passed
            to both the `AIRL` and `GAIL` constructors. Duplicate keyword argument keys
            between `algorithm_kwargs["shared"]` and `algorithm_kwargs["airl"]` (or
            "gail") leads to an error.
        wandb_integration: If True, then save the experiment logs to wandb.
        video_tracking: If True, then save the video tracking data.

    Returns:
        A dictionary with two keys. "imit_stats" gives the return value of
        `rollout_stats()` on rollouts test-reward-wrapped environment, using the final
        policy (remember that the ground-truth reward can be recovered from the
        "monitor_return" key). "expert_stats" gives the return value of
        `rollout_stats()` on the expert demonstrations loaded from `rollout_path`.

    Raises:
        ValueError: `gen_batch_size` not divisible by `num_vec`.
        ValueError: `algorithm_kwargs` included unsupported key
            (not one of "shared", "gail" or "airl").
        ValueError: Number of expert trajectories is less than `n_expert_demos`.
        FileNotFoundError: `rollout_path` does not exist.
    """
    if gen_batch_size % num_vec != 0:
        raise ValueError(
            f"num_vec={num_vec} must evenly divide gen_batch_size={gen_batch_size}.",
        )

    allowed_keys = {"shared", "gail", "airl"}
    if not algorithm_kwargs.keys() <= allowed_keys:
        raise ValueError(
            f"Invalid algorithm_kwargs.keys()={algorithm_kwargs.keys()}. "
            f"Allowed keys: {allowed_keys}",
        )

    if not os.path.exists(rollout_path):
        raise FileNotFoundError(f"File at rollout_path={rollout_path} does not exist.")

    expert_trajs = types.load(rollout_path)
    if n_expert_demos is not None:
        if len(expert_trajs) < n_expert_demos:
            raise ValueError(
                f"Want to use n_expert_demos={n_expert_demos} trajectories, but only "
                f"{len(expert_trajs)} are available via {rollout_path}.",
            )
        expert_trajs = expert_trajs[:n_expert_demos]
    expert_transitions = rollout.flatten_trajectories(expert_trajs)

    total_timesteps = int(total_timesteps)

    custom_writers = []
    if wandb_integration:
        wb_options = get_wb_options()
        writer = WandbOutputFormat(wb_options=wb_options, config=_config)
        custom_writers.append(writer)
    custom_logger = logger.configure(
        folder=osp.join(log_dir, "rl"),
        format_strs=["tensorboard", "stdout"],
        custom_writers=custom_writers,
    )

    os.makedirs(log_dir, exist_ok=True)
    sacred_util.build_sacred_symlink(log_dir, _run)

    venv = util.make_vec_env(
        env_name,
        num_vec,
        seed=_seed,
        parallel=parallel,
        log_dir=log_dir,
        max_episode_steps=max_episode_steps,
        env_make_kwargs=env_make_kwargs,
    )

    if video_tracking:
        venv = VecVideoRecorder(
            venv,
            osp.join(log_dir, "videos"),
            record_video_trigger=lambda x: x % 5000 == 0,
            video_length=500,
        )

    gen_algo = util.init_rl(
        venv,
        **init_rl_kwargs,
    )

    algorithm_kwargs_shared = algorithm_kwargs.get("shared", {})
    algorithm_kwargs_algo = algorithm_kwargs.get(algorithm, {})
    final_algorithm_kwargs = dict(
        **algorithm_kwargs_shared,
        **algorithm_kwargs_algo,
    )

    reward_net = None
    if reward_net_cls is not None:
        reward_net_kwargs = reward_net_kwargs or {}
        reward_net = reward_net_cls(
            venv.observation_space,
            venv.action_space,
            **reward_net_kwargs,
        )

    if algorithm.lower() == "gail":
        algo_cls = gail.GAIL
    elif algorithm.lower() == "airl":
        algo_cls = airl.AIRL
    else:
        raise ValueError(f"Invalid value algorithm={algorithm}.")

    trainer = algo_cls(
        venv=venv,
        demonstrations=expert_transitions,
        gen_algo=gen_algo,
        log_dir=log_dir,
        reward_net=reward_net,
        custom_logger=custom_logger,
        **final_algorithm_kwargs,
    )

    logging.info(f"Reward network summary:\n {reward_net}")
    logging.info(f"RL algorithm: {type(trainer.gen_algo)}")
    logging.info(
        f"Imitation (generator) policy network summary:\n" f"{trainer.gen_algo.policy}",
    )
    logging.info(f"Adversarial algorithm: {algorithm}")

    def callback(round_num):
        if checkpoint_interval > 0 and round_num % checkpoint_interval == 0:
            save(trainer, os.path.join(log_dir, "checkpoints", f"{round_num:05d}"))

    trainer.train(total_timesteps, callback)

    # Save final artifacts.
    if checkpoint_interval >= 0:
        save(trainer, os.path.join(log_dir, "checkpoints", "final"))

    # Final evaluation of imitation policy.
    results = {}
    sample_until_eval = rollout.make_min_episodes(n_episodes_eval)
    trajs = rollout.generate_trajectories(
        trainer.gen_algo,
        trainer.venv_train,
        sample_until=sample_until_eval,
    )
    results["expert_stats"] = rollout.rollout_stats(expert_trajs)
    results["imit_stats"] = rollout.rollout_stats(trajs)

    venv.close()

    return results


def main_console():
    observer = FileStorageObserver(osp.join("output", "sacred", "train_adversarial"))
    train_adversarial_ex.observers.append(observer)
    train_adversarial_ex.run_commandline()


if __name__ == "__main__":  # pragma: no cover
    main_console()
