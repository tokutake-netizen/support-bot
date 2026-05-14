# Deployment template

Copy this directory to start a new server deployment:

```bash
cp -r deployments/template deployments/<your-server>
cp deployments/<your-server>/.env.example deployments/<your-server>/.env
# edit .env with your IDs/keys
# place service_account.json into deployments/<your-server>/credentials/
```

Then start the bot:

```bash
python main.py --env-dir deployments/<your-server>
```

Each deployment is fully isolated: separate Discord bot token, API keys, state and logs.
