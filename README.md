# 28 CONCRETE Telegram Bot (English Version)

This is the fully English version of the Telegram bot for **28 CONCRETE**.

## Features

- Sales menu in English
- Production calculator in English
- 10 certified concrete recipes
  - 3000 PSI: Air / Non-Air
  - 3500 PSI: Air / Non-Air
  - 4000 PSI: Air / Non-Air
  - 4500 PSI: Air / Non-Air
  - 5000 PSI: Air / Non-Air
- Password protection for the production area
- Accepts `7.15` and `7,15`
- Calculates total cubic meters for ELKON
- Shows recipe per **1 m³**
- Shows total materials for the full order
- Shows each cycle separately
- Warns if the last cycle is smaller than the configured minimum
- Stores order history in PostgreSQL
- Render-ready webhook deployment

## Main Menus

### Sales Menu
- Retail Prices
- FOB Prices
- Additives
- Short Load Fee
- Hours & Terms

### Production Menu (password protected)
- New Calculation
- History
- Logout

## Required Render Environment Variables

- `BOT_TOKEN`
- `WEBHOOK_SECRET`
- `OPERATOR_PASSWORD`
- `DATABASE_URL` (normally added automatically by Render)

## Deploy on Render

1. Upload these files to GitHub.
2. In Render choose **New → Blueprint**.
3. Connect the repository.
4. Enter:
   - `BOT_TOKEN`
   - `WEBHOOK_SECRET`
   - `OPERATOR_PASSWORD` = `Aslan`
5. Deploy.

## Health Check

Open the root URL of the service. It should return JSON with status `ok`.
