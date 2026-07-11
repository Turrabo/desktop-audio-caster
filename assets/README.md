# Bundled assets

- `MaterialIconsRound-Regular.otf` — Material Icons (Round), from
  [google/material-design-icons](https://github.com/google/material-design-icons),
  licensed Apache License 2.0. Loaded process-private at runtime
  (see `streamer/ui/fonts.py`); never installed system-wide.
- `Roboto-Regular.ttf`, `Roboto-Medium.ttf` — Roboto (hinted build, v2.136), from
  [googlefonts/roboto](https://github.com/googlefonts/roboto), licensed
  Apache License 2.0. Loaded the same process-private way; the UI falls back
  to Segoe UI when missing.
