# Local Chair model

The included chair-model.json contains the trained local decision classifier. A fresh application database loads it automatically. The feature schema is validated before importing weights. This is a numerical classifier with feature-contribution explanations, not a fine-tuned language reasoning model.

The historical starting checkpoint has 11,280 examples from six selected assets in 2021–2024. Each signal includes held and not-held contexts, so examples are correlated. The historical validation accuracy is 50.2% versus a 47.0% majority-label baseline. Historical news and fundamentals were unavailable for this training. Scores are uncalibrated and do not establish profitable trading.

While the app runs, the learner checks matured outcomes every five minutes. It retrains after ten newly labelled outcomes or twenty newly eligible observations. Unverified local predictions are excluded from training targets. Teacher decisions can teach imitation immediately. Trained parameters persist in SQLite and are saved atomically to chair-model.json. Download the checkpoint and training data from Home.

Risk checks, position sizing, approvals, protective exits and daily settlement still apply. Funding and payments use demo mode or the PayPal sandbox.
