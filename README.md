# Montrella Swimming Calculator API

A Python backend that runs the exact XGBoost models from the Colab notebook, deployed on Render so your Base44 website can call it.

---

## How it works

- Pure Python tree-walking replicates XGBoost predictions with **zero approximation**
- Predictions match the Colab notebook output exactly
- Admin endpoints let you add new swimmer data and retrain the model

---

## Deploying to Render (step by step)

### 1. Push this folder to GitHub

```bash
cd swimming-api
git init
git add .
git commit -m "Initial swimming calculator API"
```

Create a new repo on GitHub (e.g. `montrella-swimming-api`), then:

```bash
git remote add origin https://github.com/YOUR_USERNAME/montrella-swimming-api.git
git push -u origin main
```

### 2. Create the Render service

1. Go to [render.com](https://render.com) and sign in
2. Click **New → Web Service**
3. Connect your GitHub repo
4. Render will auto-detect `render.yaml` — click **Deploy**
5. Wait ~3 minutes for the build to finish
6. Your API URL will be: `https://montrella-swimming-api.onrender.com`

---

## API Endpoints

### `POST /predict`
Predict competition times from test paces.

**Request:**
```json
{
  "free":   65,
  "back":   71.6,
  "breast": 87.5,
  "fly":    82.5
}
```
All fields optional — provide at least one.

**Response:**
```json
{
  "predictions": {
    "Free_50": {
      "predicted": "0:24.70",
      "lower":     "0:24.39",
      "upper":     "0:25.01",
      "predicted_seconds": 24.7,
      "mae_seconds": 0.31
    },
    ...
  }
}
```

---

### `POST /admin/add-swimmer`
Add a new swimmer's test paces and actual competition times.

**Request:**
```json
{
  "name": "Jane Doe",
  "test": {
    "free":   65.0,
    "back":   71.6,
    "breast": 87.5,
    "fly":    82.5
  },
  "actual": {
    "Free_50":  24.5,
    "Free_100": 53.1,
    "Back_100": 58.0
  }
}
```

---

### `POST /admin/retrain`
Retrain the model with all stored new swimmer data.
No request body needed.

---

### `GET /admin/export-data`
Export all stored new swimmer data as JSON (for backup or re-importing into Colab).

---

## Connecting to Base44

In your Base44 backend, replace the local `predictTimes()` call with:

```javascript
const response = await fetch("https://montrella-swimming-api.onrender.com/predict", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ free, back, breast, fly })
});
const data = await response.json();
// data.predictions.Free_50.predicted === "0:24.70"
```

---

## Retraining workflow

1. Users' test sets + times get entered through Base44 admin panel
2. Base44 calls `POST /admin/add-swimmer` for each new entry
3. When you're ready to retrain, Base44 calls `POST /admin/retrain`
4. The model updates automatically — no Colab needed for incremental updates
5. For a full retrain with the original dataset, export data via `GET /admin/export-data`, add to your Colab spreadsheet, and re-run
