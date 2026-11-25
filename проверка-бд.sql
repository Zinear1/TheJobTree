-- 1. Проверка рейтинга пользователей
SELECT * FROM user_ratings ORDER BY avg_rating DESC;

-- 2. Фильтр активных объявлений по софт-скиллам
SELECT DISTINCT a.ad_id, a.title, a.budget, a.city, c.name AS category
FROM ads a
JOIN categories c ON a.category_id = c.category_id
JOIN ad_skills ask ON a.ad_id = ask.ad_id
JOIN skills s ON ask.skill_id = s.skill_id
WHERE a.status = 'активно'
  AND s.name IN ('Пунктуальность', 'Ответственность')
ORDER BY a.budget DESC;

-- 3. Проверка автосоздания чатов триггером и переписки по объявлению
SELECT ch.chat_id, a.title, u.first_name AS sender, m.body, m.sent_at
FROM chats ch
JOIN responses r ON ch.response_id = r.response_id
JOIN ads a ON r.ad_id = a.ad_id
JOIN messages m ON ch.chat_id = m.chat_id
JOIN users u ON m.sender_id = u.user_id
ORDER BY ch.chat_id, m.sent_at;