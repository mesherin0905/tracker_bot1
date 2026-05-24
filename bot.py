            f"PnL: {s['total_pnl']:+.2f}$ | Объём: ${s['total_volume']:,.2f}\n"
            f"Лучшая: +${s['best_trade']:,.2f} | Худшая: ${s['worst_trade']:,.2f}",
            parse_mode='HTML'
        )
        return

# ==================== ЗАПУСК ================================
async def main():
    global http_session, last_known_fills

    if not TOKEN or not CHAT_ID:
        log.error("❌ TELEGRAM_TOKEN или ADMIN_ID не заданы в .env")
        return

    http_session = aiohttp.ClientSession()
    last_known_fills = load_fills_state()
    log.info(f"Загружено состояние fills для {len(last_known_fills)} кошельков")

    app = Application.builder().token(TOKEN).build()

    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("add", "Добавить кошелёк"),
        BotCommand("remove", "Удалить кошелёк"),
        BotCommand("list", "Список кошельков"),
        BotCommand("status", "Открытые позиции"),
        BotCommand("stats", "Статистика сделок"),
        BotCommand("pause", "Пауза мониторинга"),
        BotCommand("resume", "Возобновить мониторинг"),
        BotCommand("help", "Помощь"),
    ])

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", lambda u, c: cmd_list(u, c)))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, keyboard_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    await app.initialize()
    await app.start()

    poll = asyncio.create_task(
        app.updater.start_polling(drop_pending_updates=True)
    )
    monitor = asyncio.create_task(monitoring_loop(app.bot))

    # ✅ Колбэк на случай падения задач
    def handle_task_exception(task: asyncio.Task):
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Задача упала: {e}")

    poll.add_done_callback(handle_task_exception)
    monitor.add_done_callback(handle_task_exception)

    log.info("✅ Бот запущен!")

    # ✅ Основной цикл — следит за задачами и перезапускает мониторинг
    try:
        while True:
            await asyncio.sleep(60)
            if monitor.done():
                log.warning("⚠️ Мониторинг остановился! Перезапускаю...")
                monitor = asyncio.create_task(monitoring_loop(app.bot))
                monitor.add_done_callback(handle_task_exception)
            if poll.done():
                log.warning("⚠️ Polling остановился! Перезапускаю...")
                poll = asyncio.create_task(
                    app.updater.start_polling(drop_pending_updates=True)
                )
                poll.add_done_callback(handle_task_exception)
    except asyncio.CancelledError:
        log.info("Получен сигнал остановки")
    except Exception as e:
        log.error(f"Критическая ошибка main: {e}")
    finally:
        log.info("Останавливаю бота...")
        monitor.cancel()
        poll.cancel()
        try:
            await app.updater.stop()
        except:
            pass
        await app.stop()
        await app.shutdown()
        if http_session and not http_session.closed:
            await http_session.close()
        log.info("✅ Бот остановлен.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен вручную")
    except Exception as e:
        log.error(f"Критическая ошибка: {e}")
