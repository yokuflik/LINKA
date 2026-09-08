"""
Messaging domain, split from the old monolithic services/message_service.py by
responsibility. services.message_service stays as a thin facade re-exporting the
public API so main.py / routers / chat_service keep importing from one place.

Modules:
- errors            - the exception types raised across the messaging flow
- media_validation  - _validate_media + MediaAttachment (storage HEAD, per-kind limits)
- send              - process_outgoing, send_system_message, fan_out_message
- edit_delete       - edit_message, delete_message
- receipts          - mark_as_delivered/read/played, get_message_receipts
- read_api          - get_message_history (attaches derived status + media_url)
"""
