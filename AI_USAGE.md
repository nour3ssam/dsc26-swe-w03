# AI usage

Keep all three headings exactly as they are. If this file is missing or any heading is missing, the grader stops and gives 0 ("AI_USAGE.md missing or incomplete"). The content is not scored, but your Track Mentor reads it and the W8 defence can ask about it.

## Tools

- Gemini

## What AI got wrong

- The AI initially returned status code 403 instead of 404 when attempting to spend from someone else's account. I noticed this when `test_cannot_spend_from_someone_elses_account` failed during `pytest`.
- The AI missed the `Idempotent-Replayed: true` header in the idempotency response, causing `test_retry_with_same_key_does_not_pay_twice` to fail.

## In my own words

1. Why does a transfer to someone else's account return 404 and not 403?
Returning 404 prevents account enumeration (information leakage). If the API returned 403, an attacker could probe random account IDs to discover which ones exist in the system. Returning 404 ensures non-existent accounts and unauthorized accounts look identical to an attacker.

2. What would happen to a customer if your idempotency record were written in a separate transaction after the transfer?
If the transfer commits successfully but the application crashes right before committing the separate idempotency transaction, the idempotency key won't be saved. If the customer retries the request with the same idempotency key, the server won't detect the duplicate and will execute a second transfer, charging the customer twice.