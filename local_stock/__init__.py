"""Local (in-store) stock and Click & Collect monitoring — Phase 29.

Deliberately separate from engine/ and app/notify.py's mature, heavily
tested ONLINE monitoring pipeline (ObservationRecord/EventRecord/
change_detection): local stock is a different domain (per *store*, not
per *listing*), confirmed reachable for exactly two retailers so far
(JouéClub, La Grande Récré — both on the same Proximis/Rbs platform, see
local_stock/rbs_platform.py), and does not need that pipeline's full
observation history — see local_stock/models.py for why a single
current-state row per (store, listing) is enough here. Notification
still goes through app/delivery.py's durable NotificationDelivery
(Phase 27) — that layer is provider/business-agnostic by design.
"""
