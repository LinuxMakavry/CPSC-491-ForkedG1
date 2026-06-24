import argparse
import json
import os

import numpy as np
import pandas as pd
import xgboost as xgb


def _train_val_split(X, y, test_size=0.2, seed=42):
    if not 0.0 < test_size < 1.0:
        raise ValueError("test_size must be between 0 and 1.")
    n = X.shape[0]
    if n < 5:
        raise ValueError("Need at least 5 rows to train a model.")

    rng = np.random.default_rng(seed)
    idx = rng.permutation(n)
    split = int(n * (1.0 - test_size))

    train_idx = idx[:split]
    val_idx = idx[split:]

    return X[train_idx], X[val_idx], y[train_idx], y[val_idx]


def _safe_divide(a, b):
    if b == 0:
        return 0.0
    return float(a) / float(b)


def _binary_auc(y_true, y_prob):
    # Returns 0.5 when AUC is undefined (all positives/all negatives).
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob).astype(float)
    pos = int((y_true == 1).sum())
    neg = int((y_true == 0).sum())
    if pos == 0 or neg == 0:
        return 0.5

    # Wilcoxon-Mann-Whitney rank-sum formula for AUC — no sklearn dependency
    order = np.argsort(y_prob)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(y_prob) + 1, dtype=float)
    sum_pos_ranks = float(ranks[y_true == 1].sum())
    auc = (sum_pos_ranks - (pos * (pos + 1) / 2.0)) / float(pos * neg)
    return float(auc)


def _compute_metrics(y_true, y_prob, threshold=0.5):
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob).astype(float)
    y_pred = (y_prob >= threshold).astype(int)

    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    tn = int(((y_pred == 0) & (y_true == 0)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())

    accuracy = _safe_divide(tp + tn, len(y_true))
    precision = _safe_divide(tp, tp + fp)
    recall = _safe_divide(tp, tp + fn)
    f1 = _safe_divide(2 * precision * recall, precision + recall)
    roc_auc = _binary_auc(y_true, y_prob)

    return {
        "accuracy": float(accuracy),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "roc_auc": float(roc_auc),
        "confusion_matrix": {
            "tp": tp,
            "tn": tn,
            "fp": fp,
            "fn": fn,
        },
    }


def _majority_baseline_metrics(y_true):
    y_true = np.asarray(y_true).astype(int)
    pos = int((y_true == 1).sum())
    neg = int((y_true == 0).sum())
    majority = 1 if pos >= neg else 0
    y_pred = np.full_like(y_true, fill_value=majority)
    y_prob = y_pred.astype(float)
    return _compute_metrics(y_true, y_prob, threshold=0.5)


def train_xgboost(dataset_csv, model_out, metrics_out=None, test_size=0.2, seed=42):
    df = pd.read_csv(dataset_csv)
    if "win" not in df.columns:
        raise ValueError("Dataset must include a 'win' target column.")

    y = df["win"].astype(int).to_numpy()  # binary label: 1 = blue side wins
    FEATURE_COLS = ["gold_diff", "kill_diff", "assist_diff", "cs_diff",
                    "vision_diff", "tower_diff", "dragon_diff", "baron_diff"]
    # Only use columns actually present in the dataset (graceful degradation)
    available = [c for c in FEATURE_COLS if c in df.columns]
    X = df[available].to_numpy(dtype=float)

    X_train, X_val, y_train, y_val = _train_val_split(
        X, y, test_size=test_size, seed=seed
    )

    dtrain = xgb.DMatrix(X_train, label=y_train)
    dval = xgb.DMatrix(X_val, label=y_val)

    params = {
        "objective": "binary:logistic",  # outputs probability in [0, 1]
        "eval_metric": "logloss",         # log-loss used for early stopping decisions
        "max_depth": 5,                   # limits tree depth to reduce overfitting
        "eta": 0.1,                       # learning rate (shrinkage)
        "subsample": 0.9,                 # fraction of rows sampled per tree
        "colsample_bytree": 0.9,          # fraction of features sampled per tree
        "seed": seed,
    }

    evals = [(dtrain, "train"), (dval, "val")]
    model = xgb.train(
        params=params,
        dtrain=dtrain,
        num_boost_round=300,          # maximum number of trees
        evals=evals,
        early_stopping_rounds=25,     # stop if val logloss doesn't improve for 25 rounds
        verbose_eval=False,
    )

    preds = model.predict(dval)
    model_metrics = _compute_metrics(y_val, preds)
    baseline_metrics = _majority_baseline_metrics(y_val)

    os.makedirs(os.path.dirname(model_out) or ".", exist_ok=True)
    model.save_model(model_out)

    results = {
        "rows": df.shape[0],
        "features": X.shape[1],
        "best_iteration": model.best_iteration,
        "validation_metrics": model_metrics,
        "baseline_metrics": baseline_metrics,
        "model_out": model_out,
    }

    if metrics_out:
        os.makedirs(os.path.dirname(metrics_out) or ".", exist_ok=True)
        with open(metrics_out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        results["metrics_out"] = metrics_out

    return results


def main():
    current_dir = os.path.abspath(os.path.dirname(__file__))
    default_model_path = os.path.join(current_dir, "models", "latest_xgb.json")
    default_metrics_path = os.path.join(current_dir, "models", "latest_metrics.json")

    parser = argparse.ArgumentParser(description="Train XGBoost model for match outcomes.")
    parser.add_argument(
        "--dataset",
        default=os.path.join(current_dir, "data", "train.csv"),
        help="Path to dataset CSV produced by dataset_builder.py",
    )
    parser.add_argument(
        "--model-out",
        default=default_model_path,
        help="Output path for the trained model",
    )
    parser.add_argument(
        "--metrics-out",
        default=default_metrics_path,
        help="Output path for metrics JSON report",
    )
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    results = train_xgboost(
        dataset_csv=args.dataset,
        model_out=args.model_out,
        metrics_out=args.metrics_out,
        test_size=args.test_size,
        seed=args.seed,
    )

    print("Training complete.")
    print("Rows:", results["rows"])
    print("Features:", results["features"])
    print("Best iteration:", results["best_iteration"])
    vm = results["validation_metrics"]
    bm = results["baseline_metrics"]
    print("Validation accuracy:", f"{vm['accuracy']:.4f}")
    print("Validation precision:", f"{vm['precision']:.4f}")
    print("Validation recall:", f"{vm['recall']:.4f}")
    print("Validation f1:", f"{vm['f1']:.4f}")
    print("Validation roc_auc:", f"{vm['roc_auc']:.4f}")
    print("Baseline accuracy:", f"{bm['accuracy']:.4f}")
    print("Baseline f1:", f"{bm['f1']:.4f}")
    print("Model saved to:", results["model_out"])
    if "metrics_out" in results:
        print("Metrics saved to:", results["metrics_out"])


if __name__ == "__main__":
    main()
