"""
Asynchronous outgoing-message pipeline.

The WebSocket ``send_message`` handler no longer persists the message or fans
it out inline. It enqueues a small payload onto a Redis Stream
(``send_queue.enqueue_outgoing_message``) and ACKs ``{"status": "queued"}``
immediately; ``worker.run_forever`` drains the stream, runs the full send
flow (``message_service.process_outgoing``) and fans out.
"""
