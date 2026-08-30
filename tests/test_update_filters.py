import unittest
from datetime import datetime, timezone

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import filters

class UpdateFilterTests(unittest.TestCase):
    def test_commands_are_limited_to_new_messages(self):
        user = User(1, "Test", False, username="test")
        message = Message(
            10,
            datetime.now(timezone.utc),
            Chat(-100, "supergroup"),
            from_user=user,
            text="/oneri test",
            entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, 6)],
        )
        edited = Update(2, edited_message=message)
        self.assertFalse(filters.UpdateType.MESSAGE.check_update(edited))


if __name__ == "__main__":
    unittest.main()
