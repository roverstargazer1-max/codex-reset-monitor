# Codex Reset Monitor

A small personal monitor for the public forecast at <https://codex-reset.com/>.

Every 20 minutes, GitHub Actions reads `probabilities.rounded_24h` from
`https://codex-reset.com/api/forecast`. When the value moves from `80%` or below
to strictly above `80%`, the workflow sends one Chinese email through Gmail.
It will not send another probability email until the value has returned to
`80%` or below and crosses the threshold again.

## Behaviour

- Runs at minute 7, 27, and 47 of every hour.
- Sends one alert on the first observed value above 80%.
- Stores only non-sensitive state on the `monitor-state` branch.
- Sends a fault email after three consecutive forecast failures.
- Sends a recovery email when the forecast becomes available again.
- Writes a heartbeat state commit every 30 days to keep the public repository active.
- Offers a manual test-email action that does not alter formal monitor state.

## GitHub setup

1. Push this project to a public GitHub repository.
2. Open **Settings → Secrets and variables → Actions**.
3. Create the repository secret `GMAIL_ADDRESS` with the Gmail sender address.
4. Create the repository secret `GMAIL_APP_PASSWORD` with the 16-character Google app password.
5. Open **Actions → Codex Reset Monitor → Run workflow**.
6. Select `send-test-email` and run it once.
7. Confirm receipt of the Chinese test email.

Never place the Gmail address or app password in a committed file. The Gmail
login password is not used.

## Manual local tests

```powershell
python -m unittest discover -s tests -v
```

The tests are offline and do not send email or call GitHub.

## State branch

The first scheduled or manual `check-now` run creates `monitor-state` from the
default branch and writes `monitor-state.json`. The file contains only:

- whether the monitor has been initialized;
- whether the probability is currently above the threshold;
- the consecutive forecast failure count;
- whether a failure email was sent;
- the last heartbeat timestamp.

## Security

- Gmail credentials are read only from GitHub Secrets.
- The workflow uses the built-in `GITHUB_TOKEN` with `contents: write` solely to
  maintain the state branch.
- Pull requests do not receive repository secrets.
- The monitor uses Gmail SMTP with STARTTLS.
