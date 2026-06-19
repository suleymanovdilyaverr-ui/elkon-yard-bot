# 28 CONCRETE Smart Cycle Bot

A production-ready English Telegram bot for 28 CONCRETE.

## Included features

### Two roles only

- **Administrator** — full access to production calculations, history, smart cycle optimization, cycle-by-cycle material details, roles, prices, recipes, and settings.
- **Salesperson** — access to retail prices, FOB prices, additives, short-load fees, and working terms.

There is no shared password and no Operator role. Access is assigned by Telegram ID.

### Smart Batch Optimizer

The bot first creates the standard ELKON cycle plan using the configured maximum cycle size. If the final cycle is below the configured minimum, it proposes an evenly distributed optimized plan. The administrator must explicitly choose:

- **Use Optimized Plan**
- **Keep Standard Plan**
- **Cancel**

The bot never silently changes the production plan.

### Calculation by every cycle

After the cycle plan is selected, the bot shows every cycle separately with:

- cycle volume in m³ and yd³;
- Cement, Stone, Sand, and Water;
- Sikament 475, Air, and SikaFume 290;
- total cycle weight.

### Existing functionality retained

- 10 certified concrete mixes from 3000 to 5000 PSI, Air and Non-Air;
- exact yd³ to m³ conversion;
- ELKON recipe per 1 m³;
- total materials for the order;
- PostgreSQL calculation history;
- editable prices, recipes, short-load fees, and production settings;
- Render webhook deployment.

## Required Render environment variables

- `BOT_TOKEN`
- `WEBHOOK_SECRET`
- `ADMIN_TELEGRAM_IDS` — comma-separated numeric Telegram IDs
- `DATABASE_URL` — normally linked automatically by the Render Blueprint

Example:

```text
ADMIN_TELEGRAM_IDS=123456789,987654321
```

## Role management

Administrator commands:

```text
/users
/setrole TELEGRAM_ID salesperson
/setrole TELEGRAM_ID admin
/delrole TELEGRAM_ID
```

Roles can also be managed through **Admin Settings → Users & Roles**.

## Deploy

1. Upload all project files to the root of the GitHub repository.
2. Confirm that the English file is named exactly `main.py`.
3. In Render, set the required environment variables.
4. Run **Manual Deploy → Clear build cache & deploy**.
5. After the service becomes Live, send `/start` to the Telegram bot.
