import asyncio
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile

TOKEN = os.getenv("TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(F.text == '/start')
async def send_video_handler(message: Message):
    # Путь к локальному видеофайлу
    video_file = FSInputFile("video.mp4")
    await message.answer_video(
        video=video_file,
        caption="Вот ваше видео!"
    )

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
