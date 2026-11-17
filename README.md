# Upstox Volume Surge Scanner (GitHub Actions)

Chartink ke "Volume > 3x average volume" scan ka Upstox API version + PDF wali strategy.
Educational use only. Not investment advice. Ye tool order NAHI lagata (sirf scan aur signal).

## Token: roz login ki zarurat nahi
Upstox ka **Analytics Token** use karo: 1 saal valid, free, read-only, aur Market Quote / Historical Data ke
liye static IP nahi chahiye (GitHub server par chalega). Isse order nahi lag sakte, jo is project ke liye theek hai.

## Step by step
1. **Token banao:** https://account.upstox.com/developer/apps#analytics > *Generate Token* > Confirm > copy icon.
   (Agar page app maange to pehle ek Upstox API app bana lo.)
2. **GitHub repo banao:** github.com > New repository > name `upstox-volume-scanner` > **Private**.
3. **Files upload karo:** repo > *Add file > Upload files*, zip extract karke saari files + `.github` folder daalo.
   Ya terminal se:
   ```bash
   cd upstox-volume-scanner
   git init && git add . && git commit -m "initial"
   git branch -M main
   git remote add origin https://github.com/<username>/upstox-volume-scanner.git
   git push -u origin main
   ```
4. **Secret add karo:** repo > Settings > Secrets and variables > Actions > New repository secret
   - Name: `UPSTOX_ACCESS_TOKEN`, Value: step 1 ka token.
   - Optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (signal alert ke liye).
5. **Test run:** Actions tab > *Volume Surge Scan* > Run workflow (`ignore_time = true`). Result run page ke *Summary* me.
6. **Automatic:** schedule Mon-Fri, har 10 min, 09:30-15:20 IST chalta rahega.
7. **Customize:** `universe.txt` me symbols badlo, `scan.yml` me `--risk`, `--capital` badlo.
8. **Saal me ek baar:** token expiry se pehle naya Analytics Token banao aur secret update karo.

## Dhyan rakho
- Token kabhi code/commit me mat daalo, sirf Secrets me. Repo private rakho.
- GitHub cron 5-15 min late ho sakta hai ya skip bhi. Watchlist/alert ke liye theek, tez entry ke liye nahi.
- Market holiday par scan khaali aayega.
- Pehle paper trade karo.
