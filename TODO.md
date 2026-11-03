# TODO

Ordered by priority (importance x ease). Work top to bottom.

1. `garmin.py` has no delete-workout call, so `unschedule_workout` clears the
   calendar but leaves the workout in the Garmin library. Every session that
   `fit train clear` or `refresh` takes off the calendar stays behind there.
