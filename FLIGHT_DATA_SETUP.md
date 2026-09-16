# 🚀 Flight Data Setup Guide - OpenSky API Configuration

## Problem
The bot is working but flight data is showing "Нет данных" (No data). This happens because:
- OpenSky API requires authentication for reliable access
- Public API access is heavily rate-limited and may not return flight data

## Solution: Set Up OpenSky API Authentication

### Step 1: Create OpenSky Account

1. Go to https://opensky-network.org/
2. Click "Sign up" (or register)
3. Create a free account with your email
4. Verify your email

### Step 2: Get Your API Credentials

1. After registration, log in to https://opensky-network.org/
2. Go to your profile/settings
3. Look for "API" or "Credentials" section
4. You'll find your **username** (usually your email or login)
5. Copy your **password** (or API key if they provide it)

### Step 3: Add Credentials to Railway

1. Go to your Railway dashboard: https://railway.app/
2. Open your Taxi Helper Bot project
3. Click on "Variables" or "Environment" tab
4. Add these two variables:

```
OPENSKY_USERNAME = your_opensky_username
OPENSKY_PASSWORD = your_opensky_password
```

**Example:**
```
OPENSKY_USERNAME = dmitry@gmail.com
OPENSKY_PASSWORD = mySecurePassword123
```

### Step 4: Deploy the Updated Code

1. Open terminal on your Mac
2. Navigate to your project folder:
```bash
cd ~/Desktop/taxi-bot-deploy
```

3. Pull the latest changes:
```bash
git pull origin main
```

4. Push to Railway:
```bash
git push -u origin main
```

5. Railway will automatically redeploy with the new environment variables

### Step 5: Verify It's Working

1. Open Telegram and start your Taxi Helper bot
2. Select a city (Moscow)
3. Select a category (TAXI)
4. Select Airport option
5. Click on an airport (SVO, DME, etc.)

You should now see actual flight data with departure and arrival information!

## OpenSky API Free Account Limits

- **Requests per day:** 4,000
- **Data delay:** A few hours behind real-time
- **Reliability:** Good for testing

## If It Still Doesn't Work

Check the Railway logs:

1. Go to Railway dashboard
2. Click your Taxi Helper project
3. Click "Logs" tab
4. Look for errors like:
   - `401` - Wrong username/password
   - `429` - Too many requests (wait and try again)
   - `404` - Airport not found

## Upgrading to Paid Plan (Optional)

For real-time data with no daily limits:

1. Log in to OpenSky: https://opensky-network.org/
2. Go to your profile
3. Click "Upgrade" or "Pro Account"
4. Choose a subscription plan
5. Update your Railway environment variables with new credentials

## Troubleshooting

### No flight data showing
- Check that OPENSKY_USERNAME and OPENSKY_PASSWORD are correct
- Wait a few seconds and try again (might be rate limiting)

### 401 Error
- Your username/password is incorrect
- Verify on opensky-network.org that your credentials work

### App crashes after adding variables
- Restart the Railway deployment (click "Redeploy")
- Check that environment variable names are exactly: `OPENSKY_USERNAME` and `OPENSKY_PASSWORD`

### Want to test locally?
Add these to `.env` file in your project folder:
```
OPENSKY_USERNAME=your_username
OPENSKY_PASSWORD=your_password
BOT_TOKEN=8968196261:AAGjxaTy_evirnWDAO124vmkbbDFy03kekY
```

Then run:
```bash
python3 main.py
```

---

**Questions?** Check the OpenSky API docs: https://opensky-network.org/api/
