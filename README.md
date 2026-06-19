# 28 CONCRETE Volume & Smart Cycle Bot

A production-ready English Telegram bot for 28 CONCRETE.

## Roles

The bot has only two roles:

- **Administrator** — full access to production calculations, Smart Batch Optimizer, cycle-by-cycle material details, history, users, prices, recipes, and settings.
- **Salesperson** — access to sales information and the Concrete Volume Calculator.

Access is assigned by numeric Telegram ID. There is no shared password.

## Concrete Volume Calculator

The new calculator is available from the main menu and the Sales Menu. It supports:

- Rectangular Slab / Driveway / Sidewalk
- Continuous Footing
- Concrete Wall
- Round Column / Pier
- Curb / Grade Beam
- Circular Slab

The user enters dimensions in feet and inches, quantity, and waste percentage. The bot returns:

- raw volume in cubic feet;
- base volume in cubic yards;
- waste allowance;
- exact total in yd³ and m³;
- recommended order rounded up to the nearest 0.25 yd³.

Command:

```text
/volume
```

Example for a slab:

```text
Length: 40 ft
Width: 20 ft
Thickness: 4 in
Quantity: 1
Waste: 5%

Exact total: 10.370 yd³
Recommended order: 10.50 yd³
```

## Smart Batch Optimizer

The bot first creates the standard ELKON cycle plan. If the last cycle is below the configured minimum, the administrator can choose:

- **Use Optimized Plan**
- **Keep Standard Plan**
- **Cancel**

The bot never changes a production plan without administrator confirmation.

## Calculation by every cycle

The production result includes every cycle separately with:

- volume in m³ and yd³;
- Cement, Stone, Sand, and Water;
- Sikament 475, Air, and SikaFume 290;
- total cycle weight.

## Other included functions

- 10 certified mixes from 3000 to 5000 PSI, Air and Non-Air;
- exact yd³ to m³ conversion;
- ELKON recipe per 1 m³;
- total materials for the order;
- PostgreSQL calculation history;
- editable prices, recipes, short-load fees, and production settings;
- Render webhook deployment.

## Required Render environment variables

- `BOT_TOKEN`
- `WEBHOOK_SECRET`
- `ADMIN_TELEGRAM_IDS`
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

## Deployment

1. Upload all files to the root of the GitHub repository.
2. Confirm that the main program file is named exactly `main.py`.
3. In Render, verify all required environment variables.
4. Run **Manual Deploy → Clear build cache & deploy**.
5. Wait for the service to become **Live**.
6. Send `/start` to the Telegram bot.

## Deployment verification

Open the Render service URL. The health response should contain:

```json
{
  "service": "28-concrete-volume-smart-cycle-bot",
  "version": "4.0-volume-calculator",
  "concrete_volume_calculator": true
}
```
