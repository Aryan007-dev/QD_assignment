# Rider Payout Dispute Desk

- ProcureYard take-home for the ML/AI Engineer Intern role.
- About 3–4 hours of work, due **24 hours** after you receive it.
- AI tools are allowed. Python or Node.js, any LLM provider with a free tier.
- A 30-minute call follows.

QuickDrop is a fictional delivery company with about 5,000 riders in Bengaluru, Hyderabad and Pune. Its ops team handed us the material below, as it came.

**Email from the Head of Ops**

> Subject: Fwd: rider payout complaints, please fix
>
> Every morning my three executives open WhatsApp and find hundreds of riders saying their payout is wrong. "bhai kal ka payout 120 kam aaya", "order T88213 ka surge nahi mila". They check the trips sheet, work out what the rider should have got, compare it with what we paid, and reply. Two days per reply. Riders are angry, some have quit.
>
> I want this handled automatically. If the rider is right, they get their money and a reply. If they're wrong, they get a proper explanation, not silence. Anything unclear or shady comes to my team, and my team needs to see what happened. I also need to trust it before it moves money.
>
> Meera

**Note from Finance**

> Auto-pay is fine for small amounts: up to ₹200 per dispute, once per rider per day. Bigger or repeat ones need someone from ops to approve. Never pay more than the rider is owed, we lost money on this last quarter. We reconcile against PaySwift, not against your database.

**Chat from the engineer who set up the messaging vendor**

> the vendor POSTs each rider message to us. if they don't get a 2xx in ~10s they send it again. wamid is their id for the message, rider_id comes from the phone number it was sent from.
>
> PaySwift sandbox behaves like the real thing btw. which, you'll see.

**Interface**

```
POST /messages
{ "message_id": "wamid.KAZ5TCF7RARUVH", "rider_id": "R003",
  "text": "Bhai order T926334 ka surge nahi mila, 20 tarikh wala",
  "received_at": "2026-09-22T09:05:00+05:30" }

→ { "reply": "text for the rider",
    "outcome": "pay | needs_approval | explain | ask | escalate",
    "amount": 25 }
```

`data/messages.json` has 25 example messages with the expected result for each.

**In the repo** (https://github.com/Procureyard/qd-assignment):

- `docs/policy.md`: pasted from the ops wiki.
- `data/`: exports from our systems, and the 25 sample messages.
- `payswift/`: docs for the payment provider; `docker compose up payswift` runs its sandbox.
- Everything else is yours to decide.

**Submit:**

- Clone the repo, don't fork it. Build in a new private GitHub repo of your own.
- Add `pratham-saraf` as a collaborator and reply to the email you received this in with the link, within 24 hours.
- README, one page maximum: how to run it, what you built and skipped, assumptions you made, your result on the sample messages.
- Put your AI session logs in `ai-logs/`. `SUBMISSION.md` has the logistics.
