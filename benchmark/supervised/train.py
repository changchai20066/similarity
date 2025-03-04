"Supervised loss benchmark"
from __future__ import annotations

import argparse
import gc
import json
import os
import re
from collections.abc import Mapping
from typing import Any

import tensorflow as tf
import tensorflow.keras.backend
import tensorflow.random
from components import datasets, make_augmentations, make_experiments, metrics, utils
from tabulate import tabulate
from tensorflow.keras.callbacks import EarlyStopping, ModelCheckpoint
from termcolor import cprint

from tensorflow_similarity.schedules import WarmupCosineDecay
from tensorflow_similarity.search import NMSLibSearch
from tensorflow_similarity.utils import tf_cap_memory

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "1"
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)


def run(cfg: Mapping[str, Any], filter_pattern: str) -> None:
    if cfg.get("tfds_data_dir", None):
        os.environ["TFDS_DATA_DIR"] = cfg["tfds_data_dir"]

    version = cfg["version"]
    random_seed = cfg["random_seed"]
    train_aug_fns = make_augmentations(cfg["augmentations"]["train"])
    test_aug_fns = make_augmentations(cfg["augmentations"]["test"])

    data_dir = os.path.join(cfg["dataset_dir"], version)
    benchmark_dir = os.path.join(cfg["benchmark_dir"], version)

    p = re.compile(filter_pattern)
    experiments = [e for e in make_experiments(cfg, benchmark_dir) if p.match(e.run_grp)]

    for exp in experiments:
        cprint(f"|-{exp.run_grp}", "blue")

    cprint(f"{len(experiments)} Run Groups\n", "blue")
    if input("Would you like to continue: [Y/n] ").lower() != "y":
        cprint("Exit", "red")
        return
    else:
        cprint("Begin Training", "green")

    for exp in experiments:
        tf.random.set_seed(random_seed)

        # Load the raw dataset
        cprint(f"\n|-loading preprocessed {exp.dataset.name}\n", "blue")
        ds = datasets.utils.load_serialized_dataset(exp.dataset, data_dir)

        headers = [
            "dataset_name",
            "architecture_name",
            "loss_name",
            "opt_name",
            "training_name",
        ]
        row = [
            [
                f"{exp.dataset.name}",
                f"{exp.architecture.name}-{exp.architecture.params['embedding']}",
                f"{exp.loss.name}",
                f"{exp.opt.name}",
                f"{exp.training.name}",
            ]
        ]
        cprint(tabulate(row, headers=headers), "yellow")

        utils.clean_dir(exp.path)

        train_x, train_y = ds.get_train_ds()

        cprint("\n|-building train dataset\n", "blue")
        train_ds = datasets.utils.make_sampler(
            train_x,
            train_y,
            exp.training.params["train"],
            train_aug_fns,
        )
        exp.training.params["train"]["num_examples"] = train_ds.num_examples

        fid = next(iter(ds.fold_ids))
        cprint("\n|-building val dataset", "blue")
        cprint(
            f"|-NOTE: the val dataset is built using val from {fid} and contains examples in the training dataset\n",
            "yellow",
        )

        fold_ds = ds.get_fold_ds(fid)
        val_ds = datasets.utils.make_sampler(
            fold_ds["val"][0],
            fold_ds["val"][1],
            exp.training.params["val"],
            train_aug_fns,
        )
        exp.training.params["val"]["num_examples"] = val_ds.num_examples

        # Training params
        callbacks = [
            metrics.make_eval_callback(
                val_ds,
                exp.dataset.eval_callback.max_num_queries,
                exp.dataset.eval_callback.max_num_targets,
            ),
            ModelCheckpoint(
                exp.path,
                monitor="map@R",
                mode="max",
                save_best_only=True,
            ),
        ]

        if "steps_per_epoch" in exp.training.params:
            steps_per_epoch = exp.training.params["steps_per_epoch"]
        else:
            batch_size = train_ds.classes_per_batch * train_ds.examples_per_class_per_batch
            steps_per_epoch = train_ds.num_examples // batch_size

        if "validation_steps" in exp.training.params:
            validation_steps = exp.training.params["validation_steps"]
        else:
            batch_size = val_ds.classes_per_batch * val_ds.examples_per_class_per_batch
            validation_steps = val_ds.num_examples // batch_size

        if "epochs" in exp.training.params:
            epochs = exp.training.params["epochs"]
        else:
            epochs = 1000
            # TODO(ovallis): expose EarlyStopping params in config
            early_stopping = EarlyStopping(
                monitor="map@R",
                patience=5,
                verbose=0,
                mode="max",
                restore_best_weights=True,
            )
            callbacks.append(early_stopping)

        # TODO(ovallis): break this out into a benchmark component
        if "lr_schedule" in exp.training.params:
            batch_size = train_ds.classes_per_batch * train_ds.examples_per_class_per_batch
            total_steps = (train_ds.num_examples // batch_size) * epochs
            wu_steps = int(total_steps * exp.training.params["lr_schedule"]["warmup_pctg"])
            alpha = exp.training.params["lr_schedule"]["min_lr"] / exp.opt.params["lr"]
            exp.lr_schedule = WarmupCosineDecay(
                max_learning_rate=exp.opt.params["lr"],
                total_steps=total_steps,
                warmup_steps=wu_steps,
                alpha=alpha,
            )

        t_msg = [
            "\n|-Training",
            f"|  - Num train examples: {train_ds.num_examples}",
            f"|  - Num val examples:   {val_ds.num_examples}",
            f"|  - Steps per epoch:    {steps_per_epoch}",
            f"|  - Epochs:             {epochs}",
            f"|  - Validation steps:   {validation_steps}",
            "|  - Eval callback",
            f"|  -- Num queries:       {len(callbacks[0].queries_known)}",
            f"|  -- Num targets:       {len(callbacks[0].targets)}",
        ]
        cprint("\n".join(t_msg) + "\n", "green")

        model = utils.make_model(exp)

        history = model.fit(
            train_ds,
            steps_per_epoch=steps_per_epoch,
            epochs=epochs,
            callbacks=callbacks,
            validation_data=val_ds,
            validation_steps=validation_steps,
        )

        # Evaluation
        cprint("\n|-building eval dataset\n", "blue")
        test_x, test_y = ds.get_test_ds()
        test_x, test_y, class_counts = datasets.utils.make_eval_data(test_x, test_y, test_aug_fns)

        eval_metrics = metrics.make_eval_metrics(cfg["evaluation"], class_counts)

        del model._index.search
        del model._index.search_type
        model._index.search_type = NMSLibSearch(
            distance=model.loss.distance,
            dim=exp.architecture.params["embedding"],
            method="brute_force",
        )
        model.reset_index()

        model.index(test_x, test_y)

        e_msg = [
            "\n|-Evaluate Retriveal Metrics",
            f"|  - Num eval examples: {len(test_x)}",
        ]
        cprint("\n".join(e_msg) + "\n", "green")
        eval_results = model.evaluate_retrieval(
            test_x,
            test_y,
            retrieval_metrics=eval_metrics,
        )

        # Save history
        with open(os.path.join(exp.path, "history.json"), "w") as o:
            o.write(json.dumps(history.history, cls=utils.NpEncoder))

        # Save eval metrics
        with open(os.path.join(exp.path, "eval_metrics.json"), "w") as o:
            o.write(json.dumps(eval_results, cls=utils.NpEncoder))

        # Ensure we release all the mem
        for c in callbacks:
            del c
        for e in eval_metrics:
            del e
        del model._index.search
        del model._index.search_type
        del model
        if exp.lr_schedule:
            del exp.lr_schedule
        del train_ds._x
        del train_ds._y
        del val_ds._x
        del val_ds._y
        del train_ds
        del val_ds
        del test_x
        del test_y
        tf.keras.backend.clear_session()
        gc.collect()

        del ds
        gc.collect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train model")
    parser.add_argument("--config", "-c", help="config path")
    parser.add_argument("--filter", "-f", help="run only the run groups that match the regexp", default=".*")
    args = parser.parse_args()

    if not args.config:
        parser.print_usage()
        quit()

    tf_cap_memory()
    gc.collect()
    tf.keras.backend.clear_session()

    config = json.loads(open(args.config).read())
    run(config, filter_pattern=args.filter)
