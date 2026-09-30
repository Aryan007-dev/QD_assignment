`riders.csv`: rider_id, name, city, joined_on

`trips.csv`: trip_id, rider_id, started_at, status, distance_km, surge_multiplier. Status is `completed`, `cancelled_by_customer` or `cancelled_by_rider`. Exported from the trips system, 12 to 21 Sep 2026.

`payout_lines.csv`: line_id, payout_date, rider_id, line_type, trip_id, amount. What riders were actually paid. Line type is `trip`, `daily_incentive` or `cancellation_penalty`; penalties are negative.

`messages.json`: sample messages, each as the vendor sends it, plus an `expected` block for your own checks. Some list `also_acceptable` results.
