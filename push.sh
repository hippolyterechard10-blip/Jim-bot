#!/usr/bin/env bash
# Quick push to GitHub — uses GITHUB_TOKEN from Replit Secrets
set -e
git add -A
MSG="${1:-Auto-sync $(date +%Y-%m-%d)}"
if git diff --cached --quiet; then
  echo "Nothing new to commit — pushing existing HEAD..."
else
  git commit -m "$MSG"
fi
git push "https://hippolyterechard10-blip:${GITHUB_TOKEN}@github.com/hippolyterechard10-blip/Jim-bot.git" main
echo "✅ Pushed to GitHub main"
