import json
import math
import os
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Load model ────────────────────────────────────────────────────────────────
MODEL_PATH = os.path.join(os.path.dirname(__file__), "xgboost_models.json")
with open(MODEL_PATH) as f:
    MODEL_DATA = json.load(f)

# Base scores derived from the trained XGBoost models
# (XGBoost stores predictions as base_score + sum of tree outputs)
BASE_SCORES = {
    "Free_50":    24.3481437109647,
    "Free_100":   52.347154682446,
    "Free_200":   111.38001080504499,
    "Free_500":   294.14403619365004,
    "Free_1000":  615.305957848355,
    "Free_1650":  1019.3853593476489,
    "Back_100":   58.9346094876,
    "Back_200":   124.786656599655,
    "Breast_100": 67.2959109137976,
    "Breast_200": 142.397008964878,
    "Fly_100":    57.806189273731995,
    "Fly_200":    123.98267315475601,
    "IM_200":     127.202251617638,
    "IM_400":     267.52077297359904,
}

# MAE values from Colab cross-validation (used for prediction ranges)
MAE = {
    "Free_50":    0.31,
    "Free_100":   0.81,
    "Free_200":   1.59,
    "Free_500":   4.87,
    "Free_1000":  8.38,
    "Free_1650":  8.98,
    "Back_100":   1.21,
    "Back_200":   2.28,
    "Breast_100": 2.63,
    "Breast_200": 3.64,
    "Fly_100":    1.53,
    "Fly_200":    4.28,
    "IM_200":     2.21,
    "IM_400":     5.32,
}

# ── In-memory store for new swimmer data (persists until server restart)
# In production you'd use a real database — for now this lets admin add data
# and retrain without needing a DB setup
new_swimmer_data = []


# ── XGBoost tree walking (pure Python, no xgboost library needed) ─────────────
def walk_tree(node, x):
    """Traverse a single XGBoost decision tree and return the leaf value."""
    if "leaf" in node:
        return node["leaf"]
    feat_idx = int(node["split"][1:])  # "f0" -> 0, "f1" -> 1, etc.
    val = x[feat_idx]
    threshold = node["split_condition"]
    if isinstance(val, float) and math.isnan(val):
        next_id = node["missing"]
    elif val < threshold:
        next_id = node["yes"]
    else:
        next_id = node["no"]
    for child in node["children"]:
        if child["nodeid"] == next_id:
            return walk_tree(child, x)


def predict_event(event, free, back, breast, fly):
    """Predict a single event time using the XGBoost model trees."""
    imp = MODEL_DATA[event]["imputer_means"]
    # Replace missing inputs with the training-data mean (same as SimpleImputer)
    x = [
        free   if free   is not None else imp[0],
        back   if back   is not None else imp[1],
        breast if breast is not None else imp[2],
        fly    if fly    is not None else imp[3],
    ]
    raw = sum(walk_tree(t, x) for t in MODEL_DATA[event]["trees"])
    return BASE_SCORES[event] + raw


def format_time(seconds):
    """Convert seconds to M:SS.ss string."""
    if seconds <= 0:
        return "0:00.00"
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m}:{s:05.2f}"


def seconds_to_dict(seconds, mae):
    """Return predicted time with lower/upper range as a dict."""
    return {
        "predicted":  format_time(seconds),
        "lower":      format_time(seconds - mae),
        "upper":      format_time(seconds + mae),
        "predicted_seconds": round(seconds, 4),
        "mae_seconds": mae,
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Montrella Swimming Calculator API"})


@app.route("/predict", methods=["POST"])
def predict():
    """
    Predict competition times from test set paces.

    Body (JSON):
    {
        "free":   65,      // freestyle pace per 100 (seconds) — optional
        "back":   71.6,    // backstroke pace per 100 — optional
        "breast": 87.5,    // breaststroke pace per 100 — optional
        "fly":    82.5     // butterfly pace per 100 — optional
    }
    At least one pace must be provided.
    """
    body = request.get_json(silent=True) or {}
    free   = body.get("free")
    back   = body.get("back")
    breast = body.get("breast")
    fly    = body.get("fly")

    if all(v is None for v in [free, back, breast, fly]):
        return jsonify({"error": "Provide at least one pace (free, back, breast, fly)"}), 400

    predictions = {}
    for event in MODEL_DATA:
        pred_seconds = predict_event(event, free, back, breast, fly)
        predictions[event] = seconds_to_dict(pred_seconds, MAE[event])

    return jsonify({"predictions": predictions})


@app.route("/admin/add-swimmer", methods=["POST"])
def add_swimmer():
    """
    Admin endpoint: add a new swimmer's test paces + actual competition times.
    This data is stored and can later be used to retrain the model.

    Body (JSON):
    {
        "name": "Jane Doe",
        "test": {
            "free":   65.0,
            "back":   71.6,
            "breast": 87.5,
            "fly":    82.5
        },
        "actual": {
            "Free_50":    24.5,
            "Free_100":   53.1,
            "Back_100":   58.0
            // ... any events you have data for
        }
    }
    """
    body = request.get_json(silent=True) or {}
    name   = body.get("name", "Unknown")
    test   = body.get("test", {})
    actual = body.get("actual", {})

    if not test or not actual:
        return jsonify({"error": "Both 'test' paces and 'actual' times are required"}), 400

    entry = {"name": name, "test": test, "actual": actual}
    new_swimmer_data.append(entry)

    return jsonify({
        "message": f"Swimmer '{name}' added successfully.",
        "total_new_swimmers": len(new_swimmer_data),
        "entry": entry,
    })


@app.route("/admin/retrain", methods=["POST"])
def retrain():
    """
    Admin endpoint: retrain the XGBoost models using the original training data
    PLUS any new swimmer data added via /admin/add-swimmer.

    Requires the original training spreadsheet data to be present.
    Returns updated model performance stats.

    Body (JSON): {} (no body required — uses stored new_swimmer_data)
    """
    global MODEL_DATA, BASE_SCORES, MAE

    if not new_swimmer_data:
        return jsonify({"error": "No new swimmer data to retrain with. Add swimmers first via /admin/add-swimmer."}), 400

    try:
        import numpy as np
        import pandas as pd
        from xgboost import XGBRegressor
        from sklearn.model_selection import LeaveOneOut
        from sklearn.metrics import mean_absolute_error
        from sklearn.impute import SimpleImputer
        import warnings
        warnings.filterwarnings("ignore")
    except ImportError as e:
        return jsonify({"error": f"Missing dependency for retraining: {e}. Install xgboost, scikit-learn, pandas."}), 500

    # Build a DataFrame from new_swimmer_data
    test_cols  = ["Test_Swim_Free", "Test_Swim_Back", "Test_Swim_Breast", "Test_Swim_Fly"]
    event_cols = list(MODEL_DATA.keys())

    rows = []
    for swimmer in new_swimmer_data:
        row = {
            "Name":           swimmer["name"],
            "Test_Swim_Free":   swimmer["test"].get("free"),
            "Test_Swim_Back":   swimmer["test"].get("back"),
            "Test_Swim_Breast": swimmer["test"].get("breast"),
            "Test_Swim_Fly":    swimmer["test"].get("fly"),
        }
        for event in event_cols:
            row[event] = swimmer["actual"].get(event)
        rows.append(row)

    df_new = pd.DataFrame(rows)

    # Load original training data embedded as JSON (the model file has imputer means
    # which encodes the training distribution — for full retrain we need original rows)
    # For now, retrain only on new data if we don't have original CSV
    # TODO: embed original training rows in a separate file for full retrain
    df_model = df_new.copy()

    new_model_data  = {}
    new_base_scores = {}
    new_mae         = {}
    loo = LeaveOneOut()
    results = {}

    for event in event_cols:
        sub = df_model[test_cols + [event]].dropna(subset=[event])
        if len(sub) < 3:
            # Not enough data — keep existing model
            new_model_data[event]  = MODEL_DATA[event]
            new_base_scores[event] = BASE_SCORES[event]
            new_mae[event]         = MAE[event]
            results[event] = {"status": "skipped", "reason": f"only {len(sub)} rows"}
            continue

        X = sub[test_cols].values
        y = sub[event].values
        imp = SimpleImputer(strategy="mean")
        X_imp = imp.fit_transform(X)

        preds = np.zeros(len(y))
        for train_idx, test_idx in loo.split(X_imp):
            m = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.1,
                             subsample=0.8, random_state=42)
            m.fit(X_imp[train_idx], y[train_idx])
            preds[test_idx] = m.predict(X_imp[test_idx])

        mae = mean_absolute_error(y, preds)

        final_model = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.1,
                                   subsample=0.8, random_state=42)
        final_model.fit(X_imp, y)

        # Export trees to JSON (same format as original Colab export)
        booster = final_model.get_booster()
        trees   = [json.loads(t) for t in booster.get_dump(dump_format="json")]

        new_model_data[event] = {
            "trees":         trees,
            "imputer_means": imp.statistics_.tolist(),
            "n_features":    4,
        }

        # Calculate base_score: predict on training set, base_score = mean(y) - mean(tree_sums)
        # XGBoost base_score is baked into the trees — derive it from a prediction
        x_sample  = X_imp[0].tolist()
        raw_sample = sum(
            _walk_tree_retrain(t, x_sample)
            for t in new_model_data[event]["trees"]
        )
        base_score = float(final_model.predict(X_imp[:1])[0]) - raw_sample

        new_base_scores[event] = base_score
        new_mae[event]         = round(mae, 2)

        results[event] = {
            "status":      "retrained",
            "mae_seconds": round(mae, 2),
            "n_swimmers":  len(sub),
        }

    # Save updated model to disk
    with open(MODEL_PATH, "w") as f:
        json.dump(new_model_data, f)

    MODEL_DATA   = new_model_data
    BASE_SCORES  = new_base_scores
    MAE          = new_mae

    return jsonify({
        "message": "Retrain complete.",
        "results": results,
        "new_swimmers_used": len(new_swimmer_data),
    })


def _walk_tree_retrain(node, x):
    """Tree walker used during retrain (same logic as walk_tree)."""
    if "leaf" in node:
        return node["leaf"]
    feat_idx = int(node["split"][1:])
    val = x[feat_idx]
    threshold = node["split_condition"]
    if isinstance(val, float) and math.isnan(val):
        next_id = node["missing"]
    elif val < threshold:
        next_id = node["yes"]
    else:
        next_id = node["no"]
    for child in node["children"]:
        if child["nodeid"] == next_id:
            return _walk_tree_retrain(child, x)


@app.route("/admin/export-data", methods=["GET"])
def export_data():
    """Admin: export all stored new swimmer data as JSON (for backup / Colab import)."""
    return jsonify({
        "new_swimmers": new_swimmer_data,
        "count": len(new_swimmer_data),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
