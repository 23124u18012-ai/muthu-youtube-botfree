# Muthu YouTube Downloader — Render Free Web Service

This package uses Telegram webhooks (not long polling) so it can run as a Render **Free Web Service**.

## Files
- `bot.py` — Telegram bot with MP3/MP4 choices, yt-dlp, FFmpeg post-processing and webhook startup.
- `Dockerfile` — installs Python, FFmpeg and Node.js.
- `requirements.txt` — Python dependencies, including webhook support.
- `render.yaml` — Render Blueprint for a free web service.

## Deploy
1. Upload these files to the root of a GitHub repository. Do not upload your bot token.
2. In Render choose **New + → Blueprint**, connect that repository, and apply the Blueprint. Alternatively create **New + → Web Service**, choose the repository, select Docker, and choose the Free plan.
3. In the service's Environment settings add `BOT_TOKEN` with the token from @BotFather.
4. Deploy. Render supplies `PORT` and `RENDER_EXTERNAL_HOSTNAME`; the app uses these to register the Telegram webhook.
5. Check Render Logs for `Starting Muthu YouTube Downloader webhook` and test `/start` in Telegram.

## Free-tier limitations
- Render free web services can spin down after a period without inbound traffic. The first Telegram update after sleeping can be delayed while the service wakes.
- Free instances have limited RAM/CPU. Large/high-resolution videos may run out of memory or exceed processing time. The bot keeps a 45 MB output-size limit; try 360p for tests.
- The local filesystem is temporary. This bot stores downloads only in a temporary folder and removes them after sending.
- This is not a guarantee of uninterrupted 24/7 delivery. Render may change free-tier limits and availability.

## Security
Keep `BOT_TOKEN` only in Render Environment Variables. If it has been exposed, revoke it with @BotFather and create a new one.
