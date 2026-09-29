# Linka — General Help Reference

Linka is a real-time messaging app, similar in spirit to WhatsApp or Telegram. You can message people one-on-one or in groups, share media and files, and search your conversation history. This reference covers everything you can do in the app itself (not the optional AI agent feature).

## Chats — 1:1 and groups

You can start a private chat with anyone by searching their exact username or phone number (there's no partial/fuzzy search — you have to know the exact handle). Opening a private chat before you've sent a message is just a "draft" — nothing is created on the other end until you actually send something.

Groups have three roles: Member, Admin, and Owner. Admins can add/remove members; only the Owner can change someone's role. If the Owner wants to leave a non-empty group, they must hand ownership to another member first. If removing someone leaves a group with zero members, the group is deleted entirely. Group name/description/photo changes post a system message announcing the change, and update live for anyone with the chat open.

You can pin any chat to the top of your list, and mute a chat for a preset duration (8 hours, 1 day, 1 week, forever) or a custom time — muting only stops push notifications while you're offline; it doesn't hide anything if you have the app open. Pin/mute state syncs across all your devices instantly.

## Sending, editing, deleting, replying, forwarding

You can reply to a specific message (shows as a quoted reference). You can edit or delete your own messages — deleting is a "soft delete" that can be restored anytime by the original sender, with no time limit. There's also a permanent "delete forever" option, but it only works on a message you've already deleted — it wipes the content for good and can't be undone.

Forwarding a message re-sends it into another chat as your own new message — there's no "Forwarded" label. You can forward to multiple chats at once.

## Media, files, and avatars

You can send photos (up to 5 MB), videos (up to 20 MB), voice messages (up to 5 MB), and general files (up to 20 MB, any file type). Images and videos show an instant blurry preview the moment they load, before you tap to actually download and view them.

You and your groups can have a profile photo. A small preview of it loads instantly everywhere; tapping it opens the full-size image. There's a per-account storage quota (currently 1 GB) covering all the media you've sent — if you hit it, uploads are blocked until you free up space by permanently deleting some files.

## Presence, typing, and read receipts

Your "online" status and typing indicator are only shown while your app is actually in the foreground on your device — closing or backgrounding the app makes you appear offline within about a minute. Presence and typing indicators are 1:1-only — they never show in groups.

Read receipts (the blue double-check) are private and asymmetric: whether *you* send read receipts to others is controlled by your own privacy setting, regardless of what the other person has chosen. You can turn this off in Settings ("Read receipts") — your messages will then always show as delivered rather than read to others, but your own unread counts still work normally. This setting, along with who can see when you're online/last-seen (one combined privacy control: everyone / your contacts / nobody), lives in the Settings menu.

You can also tap any message to see exactly who has delivered/read/played it (in a group, a full per-person breakdown), though this detailed history is only kept for 30 days.

## Search

You can search your messages two ways: inside one specific chat, or globally across every chat you're currently a member of. Search supports quoted phrases, excluding words with "-", and "as you type" prefix matching. There's also a smarter "meaning-based" (semantic) search that finds messages related to your query even if they don't share exact words — useful for vaguer questions. Both search modes support filtering by a date/time range via a calendar icon next to the search box. You can also jump to any search result to see it in context within the full conversation.

## Scheduled messages

You can schedule a message (text or media) to send at a future time — anywhere from 10 seconds to a year ahead. You can view, edit (time or caption), or cancel any of your pending scheduled messages before they fire. There's a cap of 100 pending scheduled messages per account.

## Usernames, display names, and profile

Every account gets a unique, auto-generated username, which you can change — but only a limited number of times (3) within any 14-day period, to prevent abuse. Usernames must be lowercase, start with a letter, and be 3–32 characters.

You can also optionally set a free-form display name (any language, up to 50 characters) shown instead of your username to other people; if unset, people see your username, and if that's unset too, your phone number. You can set a profile photo and an "about" text as well.

## Notifications & storage

When you're offline, muted chats don't send you a push notification (but the message still delivers normally once you reopen the app). There's a per-account storage quota for media as noted above — deleting media you've sent is the only way to reclaim space (deleting doesn't refund space; only the permanent "delete forever" option does).

## A few things that don't exist yet

Voice/video calling isn't available (tapping "Call" shows a "not available yet" message). There's a rate limit on how fast you can send messages and how many connections/devices you can have open at once (5), mainly to prevent abuse — under normal use you're unlikely to ever notice these.
