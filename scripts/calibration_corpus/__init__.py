"""Synthetic multilingual corpus for scripts.calibrate_vector_thresholds
(VECTOR_EMBEDDING_RETRY_AND_TASKTYPE_PLAN.md Part 3).

Every language module defines `TOPICS: dict[topic_id, (docs[3], keyword_query,
question_query)]` and `CONFUSERS: list[(text, against_topic_id)]`. Topic ids
are shared across languages, so labels are derived structurally:
  2 = same topic, 1 = same domain group / different topic, 0 = everything else
  (confusers are label 0 for the topic they are `against`, and for all others).
"""

GROUPS = {
    "food": ["pizza_order", "restaurant_booking", "recipe_cake", "groceries_shopping", "coffee_meet"],
    "travel": ["flight_booking", "hotel_stay", "passport_visa", "car_rental", "beach_trip"],
    "work": ["meeting_schedule", "deadline_report", "salary_raise", "job_interview", "invoice_payment"],
    "health": ["doctor_appointment", "medicine_pharmacy", "gym_workout", "sick_flu", "dentist"],
    "tech": ["wifi_password", "phone_broken", "app_bug", "laptop_buy", "password_reset"],
    "social": ["birthday_party", "wedding_invite", "soccer_game", "movie_night", "kids_school"],
}
TOPIC_GROUP = {t: g for g, ts in GROUPS.items() for t in ts}
LANGS = ["he", "en", "ar", "ru", "es", "fr"]
