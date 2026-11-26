from psycopg2 import IntegrityError, Error as PsycopgError, sql as pg_sql
import secrets
from mailer import send_verification_email
import os
import uuid
from datetime import datetime
from functools import wraps
from decimal import Decimal, InvalidOperation
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, g
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from psycopg2 import IntegrityError, Error as PsycopgError
from db import get_db_connection

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "thejobtree_fallback_secret")

UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_time_greeting():
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "Доброе утро"
    if 12 <= hour < 18:
        return "Добрый день"
    if 18 <= hour < 23:
        return "Добрый вечер"
    return "Доброй ночи"


def parse_price(raw_value):
    if raw_value is None:
        return None
    cleaned = raw_value.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


@app.template_filter("clean_price")
def clean_price_filter(val):
    if val is None:
        return ""
    s = f"{Decimal(val):.2f}"
    return s.rstrip("0").rstrip(".")


@app.template_filter("format_price")
def format_price_filter(val):
    if val is None:
        return "0"
    dec = Decimal(val)
    if dec == dec.to_integral_value():
        return f"{int(dec):,}".replace(",", " ")
    whole, frac = f"{dec:.2f}".split(".")
    return f"{int(whole):,}".replace(",", " ") + "," + frac.rstrip("0")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Войдите в аккаунт, чтобы открыть этот раздел.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def load_current_user():
    g.user = None
    g.counters = {"my_responses": 0, "my_active_ads": 0, "unread_messages": 0}
    g.available_cities = []

    user_id = session.get("user_id")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Берём города только из тех, что реально есть в БД
            cur.execute(
                """
                SELECT DISTINCT TRIM(city) AS city
                FROM (
                    SELECT city FROM ads
                    UNION
                    SELECT city FROM users
                ) AS db_cities
                WHERE city IS NOT NULL AND TRIM(city) <> ''
                ORDER BY city
                """
            )
            g.available_cities = [row["city"] for row in cur.fetchall()]

            if user_id:
                cur.execute(
                    """
                    SELECT u.user_id, u.first_name, u.last_name, u.email,
                           u.phone, u.city, u.avatar_url, u.bio,
                           COALESCE(u.balance, 0.00) AS balance,
                           COALESCE(ur.avg_rating, 0.00) AS avg_rating,
                           COALESCE(ur.reviews_count, 0) AS reviews_count
                    FROM users u
                    LEFT JOIN user_ratings ur ON u.user_id = ur.user_id
                    WHERE u.user_id = %s
                    """,
                    (user_id,),
                )
                g.user = cur.fetchone()

                if g.user:
                    cur.execute(
                        "SELECT COUNT(*) AS cnt FROM responses WHERE applicant_id = %s",
                        (user_id,),
                    )
                    g.counters["my_responses"] = cur.fetchone()["cnt"]

                    cur.execute(
                        "SELECT COUNT(*) AS cnt FROM ads WHERE employer_id = %s AND status = 'активно'",
                        (user_id,),
                    )
                    g.counters["my_active_ads"] = cur.fetchone()["cnt"]

                    cur.execute(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM messages m
                        JOIN chats ch ON m.chat_id = ch.chat_id
                        JOIN responses r ON ch.response_id = r.response_id
                        JOIN ads a ON r.ad_id = a.ad_id
                        WHERE (a.employer_id = %s OR r.applicant_id = %s)
                          AND m.sender_id <> %s
                          AND m.is_read = FALSE
                        """,
                        (user_id, user_id, user_id),
                    )
                    g.counters["unread_messages"] = cur.fetchone()["cnt"]
    finally:
        conn.close()

    selected_city = session.get("selected_city")
    if selected_city not in g.available_cities:
        if g.user and g.user["city"] in g.available_cities:
            selected_city = g.user["city"]
        elif g.available_cities:
            selected_city = g.available_cities[0]
        else:
            selected_city = ""
        session["selected_city"] = selected_city
    g.selected_city = selected_city


@app.route("/set-city", methods=["POST"])
def set_city():
    city = request.form.get("city", "").strip()
    if city in g.available_cities:
        session["selected_city"] = city
    else:
        flash("Выберите существующий город из списка.", "error")
    return redirect(request.referrer or url_for("index"))


# ============================================================================
# Главная лента объявлений + фильтры + отдельная сортировка
# ============================================================================
@app.route("/")
def index():
    search_query = request.args.get("q", "").strip()
    selected_skills = [int(x) for x in request.args.getlist("skill_id") if x.isdigit()]
    category_id = request.args.get("category_id", type=int)
    min_budget_str = request.args.get("min_budget", "").strip()
    max_budget_str = request.args.get("max_budget", "").strip()
    min_budget = parse_price(min_budget_str)
    max_budget = parse_price(max_budget_str)
    only_remote = request.args.get("is_remote") == "1"
    sort_by = request.args.get("sort", "date_desc")

    sort_map = {
        "date_desc":   "a.created_at DESC, a.ad_id DESC",
        "date_asc":    "a.created_at ASC, a.ad_id ASC",
        "budget_desc": "a.budget DESC, a.created_at DESC",
        "budget_asc":  "a.budget ASC, a.created_at DESC",
        "rating_desc": "COALESCE(ur.avg_rating, 0.00) DESC, a.created_at DESC",
        "rating_asc":  "COALESCE(ur.avg_rating, 0.00) ASC, a.created_at DESC",
    }
    if sort_by not in sort_map:
        sort_by = "date_desc"
    order_clause = sort_map[sort_by]

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT skill_id, name FROM skills WHERE skill_type = 'soft' ORDER BY name"
            )
            soft_skills = cur.fetchall()

            cur.execute("SELECT category_id, name FROM categories ORDER BY name")
            categories = cur.fetchall()

            sql = """
                SELECT
                    a.ad_id, a.title, a.description, a.budget, a.city,
                    a.is_remote, a.deadline_date, a.status, a.created_at,
                    c.name AS category_name,
                    u.user_id AS employer_id,
                    u.first_name AS employer_first_name,
                    u.last_name AS employer_last_name,
                    COALESCE(ur.avg_rating, 0.00) AS employer_rating,
                    COALESCE(
                        ARRAY_AGG(DISTINCT s.name) FILTER (WHERE s.skill_id IS NOT NULL),
                        '{}'
                    ) AS skills
                FROM ads a
                JOIN categories c ON a.category_id = c.category_id
                JOIN users u ON a.employer_id = u.user_id
                LEFT JOIN user_ratings ur ON u.user_id = ur.user_id
                LEFT JOIN ad_skills ask ON a.ad_id = ask.ad_id
                LEFT JOIN skills s ON ask.skill_id = s.skill_id
                WHERE a.status = 'активно'
                  AND a.city = %s
            """
            params = [g.selected_city]

            if search_query:
                sql += """
                    AND (
                        a.title ILIKE %s
                        OR a.description ILIKE %s
                        OR c.name ILIKE %s
                    )
                """
                like_q = f"%{search_query}%"
                params.extend([like_q, like_q, like_q])

            if category_id:
                sql += " AND (a.category_id = %s OR c.parent_id = %s)"
                params.extend([category_id, category_id])

            if min_budget is not None and min_budget >= 0:
                sql += " AND a.budget >= %s"
                params.append(min_budget)

            if max_budget is not None and max_budget >= 0:
                sql += " AND a.budget <= %s"
                params.append(max_budget)

            if only_remote:
                sql += " AND a.is_remote = TRUE"

            if selected_skills:
                sql += """
                    AND (
                        SELECT COUNT(DISTINCT ask2.skill_id)
                        FROM ad_skills ask2
                        WHERE ask2.ad_id = a.ad_id
                          AND ask2.skill_id = ANY(%s)
                    ) = %s
                """
                params.extend([selected_skills, len(selected_skills)])

            sql += f"""
                GROUP BY a.ad_id, c.name, u.user_id, ur.avg_rating
                ORDER BY {order_clause}
            """
            cur.execute(sql, params)
            ads = cur.fetchall()
    finally:
        conn.close()

    # Формируем кнопки для отдельного блока сортировки (с сохранением всех фильтров)
    def sort_url(target_sort):
        return url_for(
            "index",
            q=search_query or None,
            category_id=category_id,
            min_budget=min_budget_str or None,
            max_budget=max_budget_str or None,
            is_remote="1" if only_remote else None,
            skill_id=selected_skills or None,
            sort=target_sort,
        )

    sort_options = [
        {
            "label": "По дате",
            "hint": "сначала новые" if sort_by == "date_desc" else ("сначала старые" if sort_by == "date_asc" else ""),
            "arrow": "↓" if sort_by == "date_desc" else ("↑" if sort_by == "date_asc" else ""),
            "active": sort_by in ("date_desc", "date_asc"),
            "url": sort_url("date_asc" if sort_by == "date_desc" else "date_desc"),
        },
        {
            "label": "По цене",
            "hint": "дороже" if sort_by == "budget_desc" else ("дешевле" if sort_by == "budget_asc" else ""),
            "arrow": "↓" if sort_by == "budget_desc" else ("↑" if sort_by == "budget_asc" else ""),
            "active": sort_by in ("budget_desc", "budget_asc"),
            "url": sort_url("budget_asc" if sort_by == "budget_desc" else "budget_desc"),
        },
        {
            "label": "По рейтингу",
            "hint": "выше" if sort_by == "rating_desc" else ("ниже" if sort_by == "rating_asc" else ""),
            "arrow": "↓" if sort_by == "rating_desc" else ("↑" if sort_by == "rating_asc" else ""),
            "active": sort_by in ("rating_desc", "rating_asc"),
            "url": sort_url("rating_asc" if sort_by == "rating_desc" else "rating_desc"),
        },
    ]

    return render_template(
        "index.html",
        greeting=get_time_greeting(),
        ads=ads,
        soft_skills=soft_skills,
        categories=categories,
        search_query=search_query,
        selected_skills=selected_skills,
        category_id=category_id,
        min_budget_str=min_budget_str,
        max_budget_str=max_budget_str,
        only_remote=only_remote,
        sort_by=sort_by,
        sort_options=sort_options,
    )


# ============================================================================
# Объявления (CRUD), отклики и отзывы по объявлению
# ============================================================================
@app.route("/ads/<int:ad_id>")
def ad_detail(ad_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    a.*,
                    c.name AS category_name,
                    emp.first_name AS employer_first_name,
                    emp.last_name AS employer_last_name,
                    emp.avatar_url AS employer_avatar,
                    emp.city AS employer_city,
                    emp.phone AS employer_phone,
                    COALESCE(ur_emp.avg_rating, 0.00) AS employer_rating,
                    COALESCE(ur_emp.reviews_count, 0) AS employer_reviews_count,
                    wrk.first_name AS worker_first_name,
                    wrk.last_name AS worker_last_name,
                    wrk.phone AS worker_phone
                FROM ads a
                JOIN categories c ON a.category_id = c.category_id
                JOIN users emp ON a.employer_id = emp.user_id
                LEFT JOIN user_ratings ur_emp ON emp.user_id = ur_emp.user_id
                LEFT JOIN users wrk ON a.worker_id = wrk.user_id
                WHERE a.ad_id = %s
                """,
                (ad_id,),
            )
            ad = cur.fetchone()
            if not ad:
                flash("Объявление не найдено.", "error")
                return redirect(url_for("index"))

            cur.execute(
                """
                SELECT s.skill_id, s.name, s.skill_type
                FROM ad_skills ask
                JOIN skills s ON ask.skill_id = s.skill_id
                WHERE ask.ad_id = %s
                ORDER BY s.skill_type DESC, s.name
                """,
                (ad_id,),
            )
            ad_skills = cur.fetchall()

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM responses WHERE ad_id = %s",
                (ad_id,),
            )
            responses_count = cur.fetchone()["cnt"]

            is_owner = g.user is not None and g.user["user_id"] == ad["employer_id"]
            incoming_responses = []
            recommended_candidate = None
            my_response = None

            if is_owner:
                cur.execute(
                    """
                    SELECT
                        r.response_id, r.proposed_budget, r.cover_letter,
                        r.status, r.created_at,
                        ch.chat_id,
                        u.user_id AS applicant_id,
                        u.first_name, u.last_name, u.city, u.avatar_url, u.phone,
                        COALESCE(ur.avg_rating, 0.00) AS avg_rating,
                        COALESCE(ur.reviews_count, 0) AS reviews_count
                    FROM responses r
                    JOIN chats ch ON r.response_id = ch.response_id
                    JOIN users u ON r.applicant_id = u.user_id
                    LEFT JOIN user_ratings ur ON u.user_id = ur.user_id
                    WHERE r.ad_id = %s
                    ORDER BY
                        CASE r.status
                            WHEN 'принят' THEN 1
                            WHEN 'ожидает' THEN 2
                            ELSE 3
                        END,
                        COALESCE(ur.avg_rating, 0) DESC,
                        r.created_at ASC
                    """,
                    (ad_id,),
                )
                incoming_responses = cur.fetchall()
                for resp in incoming_responses:
                    if resp["status"] != "принят":
                        resp["phone"] = None

                waiting = [r for r in incoming_responses if r["status"] == "ожидает"]
                if waiting and ad["status"] == "активно":
                    recommended_candidate = max(
                        waiting,
                        key=lambda r: (
                            1 if r["city"].lower() == ad["city"].lower() else 0,
                            float(r["avg_rating"]),
                            int(r["reviews_count"]),
                        ),
                    )
            elif g.user:
                cur.execute(
                    """
                    SELECT r.*, ch.chat_id
                    FROM responses r
                    JOIN chats ch ON r.response_id = ch.response_id
                    WHERE r.ad_id = %s AND r.applicant_id = %s
                    """,
                    (ad_id, g.user["user_id"]),
                )
                my_response = cur.fetchone()

            can_see_employer_phone = is_owner or (
                my_response is not None and my_response["status"] == "принят"
            )
            if not can_see_employer_phone:
                ad["employer_phone"] = None

            if not (is_owner and ad["worker_id"]):
                ad["worker_phone"] = None

            cur.execute(
                """
                SELECT r.review_id, r.author_id, r.target_id, r.rating, r.comment, r.created_at,
                       au.first_name AS author_first_name, au.last_name AS author_last_name,
                       tu.first_name AS target_first_name, tu.last_name AS target_last_name
                FROM reviews r
                JOIN users au ON r.author_id = au.user_id
                JOIN users tu ON r.target_id = tu.user_id
                WHERE r.ad_id = %s
                ORDER BY r.created_at DESC
                """,
                (ad_id,),
            )
            ad_reviews = cur.fetchall()
    finally:
        conn.close()

    can_review = False
    already_reviewed = False
    review_target_name = None

    if g.user and ad["status"] == "выполнено" and ad["worker_id"]:
        uid = g.user["user_id"]
        if uid in (ad["employer_id"], ad["worker_id"]):
            already_reviewed = any(rev["author_id"] == uid for rev in ad_reviews)
            if not already_reviewed:
                can_review = True
                if uid == ad["employer_id"]:
                    review_target_name = f"{ad['worker_first_name']} {ad['worker_last_name']}"
                else:
                    review_target_name = f"{ad['employer_first_name']} {ad['employer_last_name']}"

    return render_template(
        "ad_detail.html",
        ad=ad,
        ad_skills=ad_skills,
        responses_count=responses_count,
        is_owner=is_owner,
        incoming_responses=incoming_responses,
        recommended_candidate=recommended_candidate,
        my_response=my_response,
        ad_reviews=ad_reviews,
        can_review=can_review,
        already_reviewed=already_reviewed,
        review_target_name=review_target_name,
    )


@app.route("/ads/new", methods=["GET", "POST"])
@login_required
def create_ad():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT category_id, name FROM categories ORDER BY name")
            categories = cur.fetchall()
            cur.execute("SELECT skill_id, name, skill_type FROM skills ORDER BY skill_type DESC, name")
            all_skills = cur.fetchall()

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            category_id = request.form.get("category_id", type=int)
            budget = parse_price(request.form.get("budget", ""))
            city = request.form.get("city", "").strip()
            is_remote = request.form.get("is_remote") == "1"
            deadline_date = request.form.get("deadline_date") or None
            selected_skills = [int(x) for x in request.form.getlist("skills") if x.isdigit()]

            if budget is None or budget <= 0:
                flash("Укажите корректную цену больше 0 (например, 1500 или 2750.50).", "error")
                return render_template(
                    "ad_form.html",
                    mode="create",
                    ad=None,
                    categories=categories,
                    all_skills=all_skills,
                    selected_skill_ids=set(selected_skills),
                )

            if city not in g.available_cities:
                flash("Выберите существующий город из списка.", "error")
                return render_template(
                    "ad_form.html",
                    mode="create",
                    ad=None,
                    categories=categories,
                    all_skills=all_skills,
                    selected_skill_ids=set(selected_skills),
                )

            if not all([title, description, category_id]):
                flash("Заполните все обязательные поля объявления.", "error")
                return render_template(
                    "ad_form.html",
                    mode="create",
                    ad=None,
                    categories=categories,
                    all_skills=all_skills,
                    selected_skill_ids=set(selected_skills),
                )

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ads (
                        employer_id, category_id, title, description,
                        budget, city, is_remote, deadline_date
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING ad_id
                    """,
                    (
                        g.user["user_id"], category_id, title, description,
                        budget, city, is_remote, deadline_date,
                    ),
                )
                new_ad_id = cur.fetchone()["ad_id"]

                for sid in selected_skills:
                    cur.execute(
                        "INSERT INTO ad_skills (ad_id, skill_id) VALUES (%s, %s)",
                        (new_ad_id, sid),
                    )
                conn.commit()

            flash("Объявление опубликовано!", "success")
            return redirect(url_for("ad_detail", ad_id=new_ad_id))
    finally:
        conn.close()

    return render_template(
        "ad_form.html",
        mode="create",
        ad=None,
        categories=categories,
        all_skills=all_skills,
        selected_skill_ids=set(),
    )


@app.route("/ads/<int:ad_id>/edit", methods=["GET", "POST"])
@login_required
def edit_ad(ad_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ads WHERE ad_id = %s", (ad_id,))
            ad = cur.fetchone()
            if not ad:
                flash("Объявление не найдено.", "error")
                return redirect(url_for("index"))

            if ad["employer_id"] != g.user["user_id"]:
                flash("Редактировать можно только собственные объявления.", "error")
                return redirect(url_for("ad_detail", ad_id=ad_id))

            cur.execute("SELECT category_id, name FROM categories ORDER BY name")
            categories = cur.fetchall()
            cur.execute("SELECT skill_id, name, skill_type FROM skills ORDER BY skill_type DESC, name")
            all_skills = cur.fetchall()
            cur.execute("SELECT skill_id FROM ad_skills WHERE ad_id = %s", (ad_id,))
            selected_skill_ids = {row["skill_id"] for row in cur.fetchall()}

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            category_id = request.form.get("category_id", type=int)
            budget = parse_price(request.form.get("budget", ""))
            city = request.form.get("city", "").strip()
            is_remote = request.form.get("is_remote") == "1"
            deadline_date = request.form.get("deadline_date") or None
            status = request.form.get("status", ad["status"])
            selected_skills = [int(x) for x in request.form.getlist("skills") if x.isdigit()]

            if status not in ("активно", "отменено"):
                status = ad["status"]

            if budget is None or budget <= 0:
                flash("Цена должна быть числом больше 0.", "error")
                return redirect(url_for("edit_ad", ad_id=ad_id))

            if city not in g.available_cities:
                flash("Выберите существующий город из списка.", "error")
                return redirect(url_for("edit_ad", ad_id=ad_id))

            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ads
                    SET category_id = %s, title = %s, description = %s,
                        budget = %s, city = %s, is_remote = %s,
                        deadline_date = %s, status = %s
                    WHERE ad_id = %s AND employer_id = %s
                    """,
                    (
                        category_id, title, description, budget, city,
                        is_remote, deadline_date, status, ad_id, g.user["user_id"],
                    ),
                )
                cur.execute("DELETE FROM ad_skills WHERE ad_id = %s", (ad_id,))
                for sid in selected_skills:
                    cur.execute(
                        "INSERT INTO ad_skills (ad_id, skill_id) VALUES (%s, %s)",
                        (ad_id, sid),
                    )
                conn.commit()

            flash("Изменения сохранены.", "success")
            return redirect(url_for("ad_detail", ad_id=ad_id))
    finally:
        conn.close()

    return render_template(
        "ad_form.html",
        mode="edit",
        ad=ad,
        categories=categories,
        all_skills=all_skills,
        selected_skill_ids=selected_skill_ids,
    )


@app.route("/ads/<int:ad_id>/delete", methods=["POST"])
@login_required
def delete_ad(ad_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ads WHERE ad_id = %s AND employer_id = %s RETURNING ad_id",
                (ad_id, g.user["user_id"]),
            )
            deleted = cur.fetchone()
            conn.commit()
            if deleted:
                flash("Объявление удалено.", "success")
            else:
                flash("Не удалось удалить объявление (нет прав или не найдено).", "error")
    finally:
        conn.close()
    return redirect(url_for("profile"))


# ============================================================================
# Завершение работы и 5-балльные взаимные отзывы
# ============================================================================
@app.route("/ads/<int:ad_id>/complete", methods=["POST"])
@login_required
def complete_ad(ad_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT complete_ad_with_payment(%s, %s) AS paid_amount",
                (ad_id, g.user["user_id"]),
            )
            paid_amount = cur.fetchone()["paid_amount"]
            conn.commit()
            flash(
                f"Заказ завершён! С вашего баланса списано {format_price_filter(paid_amount)} ₽ "
                f"и зачислено исполнителю. Теперь можно оставить взаимные отзывы.",
                "success",
            )
    except PsycopgError as err:
        conn.rollback()
        msg = err.diag.message_primary if err.diag and err.diag.message_primary else "Не удалось завершить заказ."
        flash(msg, "error")
    finally:
        conn.close()
    return redirect(request.referrer or url_for("ad_detail", ad_id=ad_id))

# ============================================================================
# Кошелёк: просмотр баланса, пополнение, вывод и история операций
# ============================================================================
@app.route("/wallet")
@login_required
def wallet():
    uid = g.user["user_id"]
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(amount) FILTER (WHERE tx_type = 'доход_с_заказа'), 0) AS total_earned,
                    COALESCE(SUM(amount) FILTER (WHERE tx_type = 'оплата_заказа'), 0)  AS total_spent,
                    COALESCE(SUM(amount) FILTER (WHERE tx_type = 'пополнение'), 0)     AS total_deposited,
                    COALESCE(SUM(amount) FILTER (WHERE tx_type = 'вывод'), 0)          AS total_withdrawn
                FROM wallet_transactions
                WHERE user_id = %s
                """,
                (uid,),
            )
            stats = cur.fetchone()

            cur.execute(
                """
                SELECT wt.tx_id, wt.ad_id, wt.tx_type, wt.amount, wt.description, wt.created_at,
                       a.title AS ad_title
                FROM wallet_transactions wt
                LEFT JOIN ads a ON wt.ad_id = a.ad_id
                WHERE wt.user_id = %s
                ORDER BY wt.created_at DESC, wt.tx_id DESC
                """,
                (uid,),
            )
            transactions = cur.fetchall()
    finally:
        conn.close()

    return render_template("wallet.html", stats=stats, transactions=transactions)


@app.route("/wallet/deposit", methods=["POST"])
@login_required
def wallet_deposit():
    amount = parse_price(request.form.get("amount", ""))
    method = request.form.get("method", "Банковская карта").strip()

    if amount is None or amount <= 0:
        flash("Укажите корректную сумму пополнения больше 0.", "error")
        return redirect(url_for("wallet"))

    if amount > Decimal("10000000"):
        flash("Максимальная сумма одного пополнения — 10 000 000 ₽.", "error")
        return redirect(url_for("wallet"))

    uid = g.user["user_id"]
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET balance = balance + %s WHERE user_id = %s",
                (amount, uid),
            )
            cur.execute(
                """
                INSERT INTO wallet_transactions (user_id, tx_type, amount, description)
                VALUES (%s, 'пополнение', %s, %s)
                """,
                (uid, amount, f"Пополнение кошелька ({method})"),
            )
            conn.commit()
            flash(f"Кошелёк пополнен на {format_price_filter(amount)} ₽!", "success")
    except PsycopgError as err:
        conn.rollback()
        msg = err.diag.message_primary if err.diag and err.diag.message_primary else "Ошибка при пополнении."
        flash(msg, "error")
    finally:
        conn.close()

    return redirect(url_for("wallet"))


@app.route("/wallet/withdraw", methods=["POST"])
@login_required
def wallet_withdraw():
    amount = parse_price(request.form.get("amount", ""))
    requisite = request.form.get("requisite", "").strip()

    if amount is None or amount <= 0:
        flash("Укажите корректную сумму вывода больше 0.", "error")
        return redirect(url_for("wallet"))

    if not requisite:
        flash("Укажите номер карты или телефон (СБП) для вывода средств.", "error")
        return redirect(url_for("wallet"))

    uid = g.user["user_id"]
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (uid,))
            current_balance = cur.fetchone()["balance"]

            if current_balance < amount:
                conn.rollback()
                flash(
                    f"Недостаточно средств для вывода. Доступно: {format_price_filter(current_balance)} ₽.",
                    "error",
                )
                return redirect(url_for("wallet"))

            cur.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s",
                (amount, uid),
            )
            cur.execute(
                """
                INSERT INTO wallet_transactions (user_id, tx_type, amount, description)
                VALUES (%s, 'вывод', %s, %s)
                """,
                (uid, amount, f"Вывод средств на реквизиты: {requisite}"),
            )
            conn.commit()
            flash(f"Заявка выполнена: {format_price_filter(amount)} ₽ выведено на {requisite}.", "success")
    except PsycopgError as err:
        conn.rollback()
        msg = err.diag.message_primary if err.diag and err.diag.message_primary else "Ошибка при выводе средств."
        flash(msg, "error")
    finally:
        conn.close()

    return redirect(url_for("wallet"))

@app.route("/ads/<int:ad_id>/review", methods=["POST"])
@login_required
def leave_review(ad_id):
    rating = request.form.get("rating", type=int)
    comment = request.form.get("comment", "").strip()

    if rating not in (1, 2, 3, 4, 5) or not comment:
        flash("Выберите оценку от 1 до 5 и напишите текст отзыва.", "error")
        return redirect(url_for("ad_detail", ad_id=ad_id))

    uid = g.user["user_id"]
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ad_id, employer_id, worker_id, status FROM ads WHERE ad_id = %s",
                (ad_id,),
            )
            ad = cur.fetchone()
            if not ad or ad["status"] != "выполнено" or not ad["worker_id"]:
                flash("Отзыв можно оставить только после завершения работы.", "error")
                return redirect(url_for("ad_detail", ad_id=ad_id))

            if uid == ad["employer_id"]:
                target_id = ad["worker_id"]
            elif uid == ad["worker_id"]:
                target_id = ad["employer_id"]
            else:
                flash("Отзыв могут оставить только участники этой сделки.", "error")
                return redirect(url_for("ad_detail", ad_id=ad_id))

            cur.execute(
                """
                INSERT INTO reviews (ad_id, author_id, target_id, rating, comment)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (ad_id, uid, target_id, rating, comment),
            )
            conn.commit()
            flash("Отзыв опубликован! Рейтинг пользователя пересчитан автоматически.", "success")
    except IntegrityError:
        conn.rollback()
        flash("Вы уже оставляли отзыв по этому объявлению.", "error")
    finally:
        conn.close()

    return redirect(url_for("ad_detail", ad_id=ad_id))


# ============================================================================
# Отклики, выбор исполнителя (accept_response) и встроенный чат
# ============================================================================
@app.route("/ads/<int:ad_id>/respond", methods=["POST"])
@login_required
def respond_to_ad(ad_id):
    cover_letter = request.form.get("cover_letter", "").strip()
    proposed_raw = request.form.get("proposed_budget", "").strip()

    if not cover_letter:
        flash("Напишите сопроводительное сообщение для работодателя.", "error")
        return redirect(url_for("ad_detail", ad_id=ad_id))

    proposed_budget = None
    if proposed_raw:
        proposed_budget = parse_price(proposed_raw)
        if proposed_budget is None or proposed_budget <= 0:
            flash("Предлагаемая сумма должна быть числом больше 0.", "error")
            return redirect(url_for("ad_detail", ad_id=ad_id))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ad_id, employer_id, status FROM ads WHERE ad_id = %s", (ad_id,))
            ad = cur.fetchone()
            if not ad:
                flash("Объявление не найдено.", "error")
                return redirect(url_for("index"))

            if ad["status"] != "активно":
                flash("На это объявление уже нельзя откликнуться.", "error")
                return redirect(url_for("ad_detail", ad_id=ad_id))

            cur.execute(
                """
                INSERT INTO responses (ad_id, applicant_id, proposed_budget, cover_letter)
                VALUES (%s, %s, %s, %s)
                RETURNING response_id
                """,
                (ad_id, g.user["user_id"], proposed_budget, cover_letter),
            )
            response_id = cur.fetchone()["response_id"]

            cur.execute("SELECT chat_id FROM chats WHERE response_id = %s", (response_id,))
            chat_id = cur.fetchone()["chat_id"]

            cur.execute(
                "INSERT INTO messages (chat_id, sender_id, body) VALUES (%s, %s, %s)",
                (chat_id, g.user["user_id"], cover_letter),
            )
            conn.commit()

        flash("Отклик отправлен! Чат с работодателем создан автоматически.", "success")
        return redirect(url_for("chat_detail", chat_id=chat_id))
    except IntegrityError:
        conn.rollback()
        flash("Вы уже отправляли отклик на это объявление.", "error")
        return redirect(url_for("ad_detail", ad_id=ad_id))
    except PsycopgError as err:
        conn.rollback()
        msg = err.diag.message_primary if err.diag and err.diag.message_primary else "Ошибка при отправке отклика."
        flash(msg, "error")
        return redirect(url_for("ad_detail", ad_id=ad_id))
    finally:
        conn.close()


@app.route("/responses/<int:response_id>/accept", methods=["POST"])
@login_required
def accept_response_route(response_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.response_id, r.ad_id, r.status AS resp_status,
                       a.employer_id, a.status AS ad_status
                FROM responses r
                JOIN ads a ON r.ad_id = a.ad_id
                WHERE r.response_id = %s
                """,
                (response_id,),
            )
            row = cur.fetchone()
            if not row or row["employer_id"] != g.user["user_id"]:
                flash("Нет прав для утверждения этого отклика.", "error")
                return redirect(url_for("index"))

            if row["ad_status"] != "активно" or row["resp_status"] != "ожидает":
                flash("Утвердить можно только ожидающий отклик по активному объявлению.", "error")
                return redirect(request.referrer or url_for("ad_detail", ad_id=row["ad_id"]))

            cur.execute("SELECT accept_response(%s)", (response_id,))
            conn.commit()
            flash("Исполнитель утверждён! Номера телефонов сторон теперь открыты в чате и профилях.", "success")
            return redirect(request.referrer or url_for("ad_detail", ad_id=row["ad_id"]))
    finally:
        conn.close()


@app.route("/responses/<int:response_id>/reject", methods=["POST"])
@login_required
def reject_response_route(response_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.response_id, r.ad_id, r.status AS resp_status, a.employer_id
                FROM responses r
                JOIN ads a ON r.ad_id = a.ad_id
                WHERE r.response_id = %s
                """,
                (response_id,),
            )
            row = cur.fetchone()
            if not row or row["employer_id"] != g.user["user_id"]:
                flash("Нет прав для отклонения этого отклика.", "error")
                return redirect(url_for("index"))

            if row["resp_status"] == "ожидает":
                cur.execute(
                    "UPDATE responses SET status = 'отклонен' WHERE response_id = %s",
                    (response_id,),
                )
                conn.commit()
                flash("Отклик отклонён.", "success")
            return redirect(request.referrer or url_for("ad_detail", ad_id=row["ad_id"]))
    finally:
        conn.close()


@app.route("/my-responses")
@login_required
def my_responses():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    r.response_id, r.proposed_budget, r.cover_letter,
                    r.status AS response_status, r.created_at,
                    ch.chat_id,
                    a.ad_id, a.title AS ad_title, a.budget AS ad_budget,
                    a.city AS ad_city, a.status AS ad_status,
                    c.name AS category_name,
                    emp.user_id AS employer_id,
                    emp.first_name AS employer_first_name,
                    emp.last_name AS employer_last_name
                FROM responses r
                JOIN chats ch ON r.response_id = ch.response_id
                JOIN ads a ON r.ad_id = a.ad_id
                JOIN categories c ON a.category_id = c.category_id
                JOIN users emp ON a.employer_id = emp.user_id
                WHERE r.applicant_id = %s
                ORDER BY r.created_at DESC
                """,
                (g.user["user_id"],),
            )
            responses = cur.fetchall()
    finally:
        conn.close()

    return render_template("my_responses.html", responses=responses)


@app.route("/chats")
@login_required
def chats_list():
    uid = g.user["user_id"]
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ch.chat_id,
                    ch.created_at AS chat_created_at,
                    r.response_id,
                    r.status AS response_status,
                    r.proposed_budget,
                    a.ad_id,
                    a.title AS ad_title,
                    a.status AS ad_status,
                    a.employer_id,
                    r.applicant_id,
                    other_u.user_id AS other_user_id,
                    other_u.first_name AS other_first_name,
                    other_u.last_name AS other_last_name,
                    other_u.avatar_url AS other_avatar,
                    last_msg.body AS last_message_body,
                    last_msg.sent_at AS last_message_time,
                    (
                        SELECT COUNT(*)
                        FROM messages m2
                        WHERE m2.chat_id = ch.chat_id
                          AND m2.sender_id <> %s
                          AND m2.is_read = FALSE
                    ) AS unread_count
                FROM chats ch
                JOIN responses r ON ch.response_id = r.response_id
                JOIN ads a ON r.ad_id = a.ad_id
                JOIN users other_u
                  ON other_u.user_id = CASE
                      WHEN a.employer_id = %s THEN r.applicant_id
                      ELSE a.employer_id
                  END
                LEFT JOIN LATERAL (
                    SELECT m.body, m.sent_at
                    FROM messages m
                    WHERE m.chat_id = ch.chat_id
                    ORDER BY m.sent_at DESC, m.message_id DESC
                    LIMIT 1
                ) last_msg ON TRUE
                WHERE a.employer_id = %s OR r.applicant_id = %s
                ORDER BY COALESCE(last_msg.sent_at, ch.created_at) DESC
                """,
                (uid, uid, uid, uid),
            )
            chats = cur.fetchall()
    finally:
        conn.close()

    return render_template("chats_list.html", chats=chats)


@app.route("/chats/<int:chat_id>")
@login_required
def chat_detail(chat_id):
    uid = g.user["user_id"]
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ch.chat_id, ch.created_at AS chat_created_at,
                    r.response_id, r.proposed_budget, r.cover_letter,
                    r.status AS response_status, r.applicant_id,
                    a.ad_id, a.title AS ad_title, a.budget AS ad_budget,
                    a.status AS ad_status, a.employer_id, a.worker_id,
                    emp.first_name AS emp_first_name, emp.last_name AS emp_last_name,
                    emp.avatar_url AS emp_avatar, emp.phone AS emp_phone,
                    app_u.first_name AS app_first_name, app_u.last_name AS app_last_name,
                    app_u.avatar_url AS app_avatar, app_u.phone AS app_phone,
                    COALESCE(ur_app.avg_rating, 0.00) AS app_rating,
                    COALESCE(ur_emp.avg_rating, 0.00) AS emp_rating
                FROM chats ch
                JOIN responses r ON ch.response_id = r.response_id
                JOIN ads a ON r.ad_id = a.ad_id
                JOIN users emp ON a.employer_id = emp.user_id
                JOIN users app_u ON r.applicant_id = app_u.user_id
                LEFT JOIN user_ratings ur_app ON app_u.user_id = ur_app.user_id
                LEFT JOIN user_ratings ur_emp ON emp.user_id = ur_emp.user_id
                WHERE ch.chat_id = %s
                """,
                (chat_id,),
            )
            chat = cur.fetchone()
            if not chat or uid not in (chat["employer_id"], chat["applicant_id"]):
                flash("Чат не найден или у вас нет к нему доступа.", "error")
                return redirect(url_for("chats_list"))

            cur.execute(
                """
                UPDATE messages
                SET is_read = TRUE
                WHERE chat_id = %s AND sender_id <> %s AND is_read = FALSE
                """,
                (chat_id, uid),
            )
            conn.commit()

            cur.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM messages m
                JOIN chats ch ON m.chat_id = ch.chat_id
                JOIN responses r ON ch.response_id = r.response_id
                JOIN ads a ON r.ad_id = a.ad_id
                WHERE (a.employer_id = %s OR r.applicant_id = %s)
                  AND m.sender_id <> %s
                  AND m.is_read = FALSE
                """,
                (uid, uid, uid),
            )
            g.counters["unread_messages"] = cur.fetchone()["cnt"]

            cur.execute(
                """
                SELECT m.message_id, m.sender_id, m.body, m.sent_at, m.is_read,
                       u.first_name, u.last_name
                FROM messages m
                JOIN users u ON m.sender_id = u.user_id
                WHERE m.chat_id = %s
                ORDER BY m.sent_at ASC, m.message_id ASC
                """,
                (chat_id,),
            )
            messages = cur.fetchall()
    finally:
        conn.close()

    is_employer = uid == chat["employer_id"]
    counterpart_phone = None
    if chat["response_status"] == "принят":
        counterpart_phone = chat["app_phone"] if is_employer else chat["emp_phone"]
    chat["emp_phone"] = None
    chat["app_phone"] = None

    return render_template(
        "chat.html",
        chat=chat,
        messages=messages,
        is_employer=is_employer,
        counterpart_phone=counterpart_phone,
    )


@app.route("/chats/<int:chat_id>/send", methods=["POST"])
@login_required
def send_message(chat_id):
    body = request.form.get("body", "").strip()
    if not body:
        flash("Сообщение не может быть пустым.", "error")
        return redirect(url_for("chat_detail", chat_id=chat_id))

    uid = g.user["user_id"]
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.employer_id, r.applicant_id
                FROM chats ch
                JOIN responses r ON ch.response_id = r.response_id
                JOIN ads a ON r.ad_id = a.ad_id
                WHERE ch.chat_id = %s
                """,
                (chat_id,),
            )
            chat = cur.fetchone()
            if not chat or uid not in (chat["employer_id"], chat["applicant_id"]):
                flash("Нет доступа к этому чату.", "error")
                return redirect(url_for("chats_list"))

            cur.execute(
                "INSERT INTO messages (chat_id, sender_id, body) VALUES (%s, %s, %s)",
                (chat_id, uid, body),
            )
            conn.commit()
    finally:
        conn.close()

    return redirect(url_for("chat_detail", chat_id=chat_id))


# ============================================================================
# Авторизация и профиль
# ============================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE LOWER(email) = %s", (email,))
                user = cur.fetchone()
        finally:
            conn.close()

        valid = False
        if user:
            if user["password_hash"].startswith("demo_hash_"):
                valid = password in ("123456", user["password_hash"])
            else:
                valid = check_password_hash(user["password_hash"], password)

        if valid:
            session["user_id"] = user["user_id"]
            session["selected_city"] = user["city"]
            flash(f"С возвращением, {user['first_name']}!", "success")
            return redirect(url_for("index"))

        flash("Неверный email или пароль.", "error")

    return render_template("auth.html", active_tab="login")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        city = request.form.get("city", "").strip()
        password = request.form.get("password", "")

        if not all([first_name, last_name, email, phone, city, password]):
            flash("Заполните все обязательные поля.", "error")
            return render_template("auth.html", active_tab="register")

        if city not in g.available_cities:
            flash("Выберите существующий город из списка.", "error")
            return render_template("auth.html", active_tab="register")

        if len(password) < 6:
            flash("Пароль должен содержать минимум 6 символов.", "error")
            return render_template("auth.html", active_tab="register")

        password_hash = generate_password_hash(password)
        code = f"{secrets.randbelow(900000) + 100000}"

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Проверяем, не заняты ли уже email или телефон
                cur.execute(
                    "SELECT 1 FROM users WHERE LOWER(email) = %s OR phone = %s LIMIT 1",
                    (email, phone),
                )
                if cur.fetchone():
                    flash("Пользователь с таким email или телефоном уже зарегистрирован.", "error")
                    return render_template("auth.html", active_tab="register")

                # Удаляем просроченные заявки и сохраняем новую заявку с кодом на 10 минут
                cur.execute("DELETE FROM email_verifications WHERE expires_at < CURRENT_TIMESTAMP")
                cur.execute(
                    """
                    INSERT INTO email_verifications (
                        email, first_name, last_name, phone, city,
                        password_hash, code, attempts, expires_at, created_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, 0,
                        CURRENT_TIMESTAMP + INTERVAL '10 minutes',
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (email) DO UPDATE SET
                        first_name    = EXCLUDED.first_name,
                        last_name     = EXCLUDED.last_name,
                        phone         = EXCLUDED.phone,
                        city          = EXCLUDED.city,
                        password_hash = EXCLUDED.password_hash,
                        code          = EXCLUDED.code,
                        attempts      = 0,
                        expires_at    = EXCLUDED.expires_at,
                        created_at    = CURRENT_TIMESTAMP
                    """,
                    (email, first_name, last_name, phone, city, password_hash, code),
                )
                conn.commit()
        finally:
            conn.close()

        sent_real, mail_error = send_verification_email(email, first_name, code)
        if mail_error:
            flash(f"Не удалось отправить письмо на {email}. Проверьте настройки SMTP в .env ({mail_error}).", "error")
            return render_template("auth.html", active_tab="register")

        session["pending_email"] = email
        if sent_real:
            flash(f"Мы отправили 6-значный код подтверждения на {email}.", "success")
        else:
            flash("SMTP не настроен: код подтверждения выведен в консоль PyCharm.", "success")

        return redirect(url_for("verify_email"))

    return render_template("auth.html", active_tab="register")


@app.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    pending_email = session.get("pending_email")
    if not pending_email:
        flash("Сначала заполните форму регистрации.", "error")
        return redirect(url_for("register"))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *,
                       (expires_at < CURRENT_TIMESTAMP) AS is_expired,
                       GREATEST(
                           0,
                           60 - FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)))::INT
                       ) AS cooldown_left
                FROM email_verifications
                WHERE email = %s
                """,
                (pending_email,),
            )
            record = cur.fetchone()

            if not record:
                session.pop("pending_email", None)
                flash("Заявка на регистрацию не найдена или истекла. Заполните форму заново.", "error")
                return redirect(url_for("register"))

            if request.method == "POST":
                entered_code = request.form.get("code", "").strip().replace(" ", "")

                if record["is_expired"]:
                    flash("Срок действия кода истёк. Нажмите «Отправить код повторно».", "error")
                    return redirect(url_for("verify_email"))

                if record["attempts"] >= 5:
                    flash("Превышено число попыток ввода. Запросите новый код.", "error")
                    return redirect(url_for("verify_email"))

                if not secrets.compare_digest(entered_code, record["code"]):
                    cur.execute(
                        "UPDATE email_verifications SET attempts = attempts + 1 WHERE email = %s",
                        (pending_email,),
                    )
                    conn.commit()
                    left = max(0, 4 - record["attempts"])
                    flash(f"Неверный код. Осталось попыток: {left}.", "error")
                    return redirect(url_for("verify_email"))

                # Код верный — переносим пользователя в основную таблицу users в одной транзакции
                try:
                    cur.execute(
                        """
                        INSERT INTO users (first_name, last_name, email, phone, password_hash, city)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING user_id
                        """,
                        (
                            record["first_name"],
                            record["last_name"],
                            record["email"],
                            record["phone"],
                            record["password_hash"],
                            record["city"],
                        ),
                    )
                    new_user_id = cur.fetchone()["user_id"]
                    cur.execute("DELETE FROM email_verifications WHERE email = %s", (pending_email,))
                    conn.commit()
                except IntegrityError:
                    conn.rollback()
                    flash("Этот email или телефон уже успел зарегистрировать другой пользователь.", "error")
                    return redirect(url_for("register"))

                session.pop("pending_email", None)
                session["user_id"] = new_user_id
                session["selected_city"] = record["city"]
                flash("Почта подтверждена! Добро пожаловать в TheJobTree.", "success")
                return redirect(url_for("profile"))
    finally:
        conn.close()

    return render_template(
        "verify_email.html",
        email=pending_email,
        cooldown_left=record["cooldown_left"],
        is_expired=record["is_expired"],
    )


@app.route("/verify-email/resend", methods=["POST"])
def resend_verification_code():
    pending_email = session.get("pending_email")
    if not pending_email:
        return redirect(url_for("register"))

    new_code = f"{secrets.randbelow(900000) + 100000}"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT first_name,
                       GREATEST(
                           0,
                           60 - FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)))::INT
                       ) AS cooldown_left
                FROM email_verifications
                WHERE email = %s
                """,
                (pending_email,),
            )
            record = cur.fetchone()
            if not record:
                session.pop("pending_email", None)
                flash("Сессия регистрации истекла. Заполните форму заново.", "error")
                return redirect(url_for("register"))

            if record["cooldown_left"] > 0:
                flash(f"Повторная отправка будет доступна через {record['cooldown_left']} сек.", "error")
                return redirect(url_for("verify_email"))

            cur.execute(
                """
                UPDATE email_verifications
                SET code = %s,
                    attempts = 0,
                    expires_at = CURRENT_TIMESTAMP + INTERVAL '10 minutes',
                    created_at = CURRENT_TIMESTAMP
                WHERE email = %s
                """,
                (new_code, pending_email),
            )
            conn.commit()
            first_name = record["first_name"]
    finally:
        conn.close()

    sent_real, mail_error = send_verification_email(pending_email, first_name, new_code)
    if mail_error:
        flash(f"Ошибка отправки письма: {mail_error}", "error")
    elif sent_real:
        flash(f"Новый код отправлен на {pending_email}.", "success")
    else:
        flash("SMTP не настроен: новый код выведен в консоль PyCharm.", "success")

    return redirect(url_for("verify_email"))


@app.route("/logout")
def logout():
    keep_city = session.get("selected_city")
    session.clear()
    if keep_city:
        session["selected_city"] = keep_city
    flash("Вы вышли из аккаунта.", "success")
    return redirect(url_for("index"))


@app.route("/profile")
@login_required
def profile():
    return redirect(url_for("user_profile", user_id=g.user["user_id"]))


@app.route("/profile/<int:user_id>")
def user_profile(user_id):
    is_own_profile = g.user is not None and g.user["user_id"] == user_id

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.user_id, u.first_name, u.last_name, u.email, u.phone,
                       u.city, u.avatar_url, u.bio, u.created_at,
                       COALESCE(ur.avg_rating, 0.00) AS avg_rating,
                       COALESCE(ur.reviews_count, 0) AS reviews_count
                FROM users u
                LEFT JOIN user_ratings ur ON u.user_id = ur.user_id
                WHERE u.user_id = %s
                """,
                (user_id,),
            )
            profile_user = cur.fetchone()
            if not profile_user:
                flash("Пользователь не найден.", "error")
                return redirect(url_for("index"))

            can_see_phone = False
            if is_own_profile:
                can_see_phone = True
            elif g.user:
                cur.execute(
                    """
                    SELECT 1
                    FROM responses r
                    JOIN ads a ON r.ad_id = a.ad_id
                    WHERE r.status = 'принят'
                      AND (
                          (a.employer_id = %s AND r.applicant_id = %s)
                          OR (a.employer_id = %s AND r.applicant_id = %s)
                      )
                    LIMIT 1
                    """,
                    (g.user["user_id"], user_id, user_id, g.user["user_id"]),
                )
                can_see_phone = cur.fetchone() is not None

            if not can_see_phone:
                profile_user["phone"] = None

            cur.execute(
                """
                SELECT s.skill_id, s.name, s.skill_type
                FROM user_skills us
                JOIN skills s ON us.skill_id = s.skill_id
                WHERE us.user_id = %s
                ORDER BY s.skill_type DESC, s.name
                """,
                (user_id,),
            )
            profile_skills = cur.fetchall()
            profile_skill_ids = {row["skill_id"] for row in profile_skills}

            all_skills = []
            if is_own_profile:
                cur.execute("SELECT skill_id, name, skill_type FROM skills ORDER BY skill_type DESC, name")
                all_skills = cur.fetchall()

            cur.execute(
                """
                SELECT a.ad_id, a.title, a.description, a.budget, a.city,
                       a.is_remote, a.deadline_date, a.status,
                       a.employer_id, a.worker_id, c.name AS category_name
                FROM ads a
                JOIN categories c ON a.category_id = c.category_id
                WHERE a.employer_id = %s OR a.worker_id = %s
                ORDER BY a.created_at DESC
                """,
                (user_id, user_id),
            )
            all_ads = cur.fetchall()

            ads_active = [a for a in all_ads if a["status"] == "активно"]
            ads_in_progress = [a for a in all_ads if a["status"] == "в работе"]
            ads_completed = [a for a in all_ads if a["status"] == "выполнено"]

            cur.execute(
                """
                SELECT r.review_id, r.rating, r.comment, r.created_at,
                       a.ad_id, a.title AS ad_title,
                       u.user_id AS author_id,
                       u.first_name AS author_first_name,
                       u.last_name AS author_last_name
                FROM reviews r
                JOIN users u ON r.author_id = u.user_id
                JOIN ads a ON r.ad_id = a.ad_id
                WHERE r.target_id = %s
                ORDER BY r.created_at DESC
                """,
                (user_id,),
            )
            reviews = cur.fetchall()
    finally:
        conn.close()

    return render_template(
        "profile.html",
        profile_user=profile_user,
        is_own_profile=is_own_profile,
        can_see_phone=can_see_phone,
        profile_skills=profile_skills,
        profile_skill_ids=profile_skill_ids,
        all_skills=all_skills,
        ads_active=ads_active,
        ads_in_progress=ads_in_progress,
        ads_completed=ads_completed,
        reviews=reviews,
    )


@app.route("/profile/edit", methods=["POST"])
@login_required
def edit_profile():
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    city = request.form.get("city", "").strip()
    phone = request.form.get("phone", "").strip()
    bio = request.form.get("bio", "").strip()
    selected_skills = request.form.getlist("skills")

    if city not in g.available_cities:
        flash("Выберите существующий город из списка.", "error")
        return redirect(url_for("profile"))

    avatar_url = g.user["avatar_url"]
    avatar_file = request.files.get("avatar")
    if avatar_file and avatar_file.filename:
        if allowed_file(avatar_file.filename):
            ext = avatar_file.filename.rsplit(".", 1)[1].lower()
            filename = secure_filename(f"user_{g.user['user_id']}_{uuid.uuid4().hex[:8]}.{ext}")
            avatar_file.save(os.path.join(UPLOAD_FOLDER, filename))
            avatar_url = f"/static/uploads/{filename}"
        else:
            flash("Допустимые форматы аватарки: PNG, JPG, JPEG, WEBP.", "error")
            return redirect(url_for("profile"))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET first_name = %s, last_name = %s, city = %s,
                    phone = %s, bio = %s, avatar_url = %s
                WHERE user_id = %s
                """,
                (first_name, last_name, city, phone, bio, avatar_url, g.user["user_id"]),
            )

            cur.execute("DELETE FROM user_skills WHERE user_id = %s", (g.user["user_id"],))
            for sid in selected_skills:
                cur.execute(
                    "INSERT INTO user_skills (user_id, skill_id) VALUES (%s, %s)",
                    (g.user["user_id"], int(sid)),
                )
            conn.commit()
            session["selected_city"] = city
            flash("Профиль успешно обновлён.", "success")
    except IntegrityError:
        conn.rollback()
        flash("Этот номер телефона уже занят другим пользователем.", "error")
    finally:
        conn.close()

    return redirect(url_for("profile"))

# ============================================================================
# Админ-панель управления базой данных (CRUD по всем таблицам PostgreSQL)
# ============================================================================
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "sssloxsss091")

ADMIN_TABLES = {
    "users": {
        "label": "Пользователи (users)",
        "pk": ["user_id"],
        "auto_pk": True,
        "display_cols": ["user_id", "first_name", "last_name", "email", "phone", "city", "balance", "created_at"],
        "fields": [
            {"name": "first_name",    "label": "Имя",           "type": "text",     "required": True},
            {"name": "last_name",     "label": "Фамилия",       "type": "text",     "required": True},
            {"name": "email",         "label": "Email",         "type": "text",     "required": True},
            {"name": "phone",         "label": "Телефон",       "type": "text",     "required": True},
            {"name": "password_hash", "label": "Пароль (хешируется автоматически)", "type": "password", "required": True},
            {"name": "city",          "label": "Город",         "type": "text",     "required": True},
            {"name": "avatar_url",    "label": "URL аватара",   "type": "text",     "required": False},
            {"name": "bio",           "label": "О себе",        "type": "textarea", "required": False},
            {"name": "balance", "label": "Баланс (₽)", "type": "decimal", "required": True},
        ],
"wallet_transactions": {
        "label": "Транзакции кошелька (wallet_transactions)",
        "pk": ["tx_id"],
        "auto_pk": True,
        "display_cols": ["tx_id", "user_id", "ad_id", "tx_type", "amount", "description", "created_at"],
        "fields": [
            {"name": "user_id",     "label": "ID пользователя", "type": "int",     "required": True},
            {"name": "ad_id",       "label": "ID объявления (или пусто)", "type": "int", "required": False},
            {"name": "tx_type",     "label": "Тип операции",    "type": "select",  "required": True, "options": ["пополнение", "вывод", "оплата_заказа", "доход_с_заказа"]},
            {"name": "amount",      "label": "Сумма (₽)",       "type": "decimal", "required": True},
            {"name": "description", "label": "Описание",        "type": "text",    "required": False},
        ],
    },
    },
    "categories": {
        "label": "Категории (categories)",
        "pk": ["category_id"],
        "auto_pk": True,
        "display_cols": ["category_id", "name", "parent_id"],
        "fields": [
            {"name": "name",      "label": "Название категории",           "type": "text", "required": True},
            {"name": "parent_id", "label": "ID родительской (или пусто)",  "type": "int",  "required": False},
        ],
    },
    "skills": {
        "label": "Навыки и софт-скиллы (skills)",
        "pk": ["skill_id"],
        "auto_pk": True,
        "display_cols": ["skill_id", "name", "skill_type", "category_id"],
        "fields": [
            {"name": "name",        "label": "Название навыка",            "type": "text",   "required": True},
            {"name": "skill_type",  "label": "Тип навыка",                 "type": "select", "required": True, "options": ["soft", "hard"]},
            {"name": "category_id", "label": "ID категории (пусто для soft)", "type": "int", "required": False},
        ],
    },
    "ads": {
        "label": "Объявления (ads)",
        "pk": ["ad_id"],
        "auto_pk": True,
        "display_cols": ["ad_id", "employer_id", "worker_id", "category_id", "title", "budget", "city", "is_remote", "status", "deadline_date"],
        "fields": [
            {"name": "employer_id",   "label": "ID работодателя", "type": "int",      "required": True},
            {"name": "worker_id",     "label": "ID исполнителя",  "type": "int",      "required": False},
            {"name": "category_id",   "label": "ID категории",    "type": "int",      "required": True},
            {"name": "title",         "label": "Заголовок",       "type": "text",     "required": True},
            {"name": "budget",        "label": "Бюджет (₽)",      "type": "decimal",  "required": True},
            {"name": "city",          "label": "Город",           "type": "text",     "required": True},
            {"name": "is_remote",     "label": "Удалённо",        "type": "bool",     "required": True},
            {"name": "deadline_date", "label": "Дедлайн",         "type": "date",     "required": False},
            {"name": "status",        "label": "Статус",          "type": "select",   "required": True, "options": ["активно", "в работе", "выполнено", "отменено"]},
            {"name": "description",   "label": "Описание",        "type": "textarea", "required": True},
        ],
    },
    "ad_skills": {
        "label": "Навыки объявлений (ad_skills)",
        "pk": ["ad_id", "skill_id"],
        "auto_pk": False,
        "display_cols": ["ad_id", "skill_id"],
        "fields": [
            {"name": "ad_id",    "label": "ID объявления", "type": "int", "required": True},
            {"name": "skill_id", "label": "ID навыка",     "type": "int", "required": True},
        ],
    },
    "user_skills": {
        "label": "Навыки пользователей (user_skills)",
        "pk": ["user_id", "skill_id"],
        "auto_pk": False,
        "display_cols": ["user_id", "skill_id"],
        "fields": [
            {"name": "user_id",  "label": "ID пользователя", "type": "int", "required": True},
            {"name": "skill_id", "label": "ID навыка",       "type": "int", "required": True},
        ],
    },
    "responses": {
        "label": "Отклики (responses)",
        "pk": ["response_id"],
        "auto_pk": True,
        "display_cols": ["response_id", "ad_id", "applicant_id", "proposed_budget", "status", "cover_letter", "created_at"],
        "fields": [
            {"name": "ad_id",           "label": "ID объявления",         "type": "int",      "required": True},
            {"name": "applicant_id",    "label": "ID соискателя",         "type": "int",      "required": True},
            {"name": "proposed_budget", "label": "Предложенная цена (₽)", "type": "decimal",  "required": False},
            {"name": "status",          "label": "Статус отклика",        "type": "select",   "required": True, "options": ["ожидает", "принят", "отклонен"]},
            {"name": "cover_letter",    "label": "Сопроводительное",      "type": "textarea", "required": True},
        ],
    },
    "chats": {
        "label": "Чаты (chats)",
        "pk": ["chat_id"],
        "auto_pk": True,
        "display_cols": ["chat_id", "response_id", "created_at"],
        "fields": [
            {"name": "response_id", "label": "ID отклика (уникальный)", "type": "int", "required": True},
        ],
    },
    "messages": {
        "label": "Сообщения (messages)",
        "pk": ["message_id"],
        "auto_pk": True,
        "display_cols": ["message_id", "chat_id", "sender_id", "body", "is_read", "sent_at"],
        "fields": [
            {"name": "chat_id",   "label": "ID чата",        "type": "int",      "required": True},
            {"name": "sender_id", "label": "ID отправителя", "type": "int",      "required": True},
            {"name": "is_read",   "label": "Прочитано",      "type": "bool",     "required": True},
            {"name": "body",      "label": "Текст сообщения","type": "textarea", "required": True},
        ],
    },
    "reviews": {
        "label": "Отзывы (reviews)",
        "pk": ["review_id"],
        "auto_pk": True,
        "display_cols": ["review_id", "ad_id", "author_id", "target_id", "rating", "comment", "created_at"],
        "fields": [
            {"name": "ad_id",     "label": "ID объявления",  "type": "int",      "required": True},
            {"name": "author_id", "label": "ID автора",      "type": "int",      "required": True},
            {"name": "target_id", "label": "ID получателя",  "type": "int",      "required": True},
            {"name": "rating",    "label": "Оценка (1–5)",   "type": "select",   "required": True, "options": ["5", "4", "3", "2", "1"]},
            {"name": "comment",   "label": "Комментарий",    "type": "textarea", "required": True},
        ],
    },
    "email_verifications": {
        "label": "Коды подтверждения (email_verifications)",
        "pk": ["verification_id"],
        "auto_pk": True,
        "display_cols": ["verification_id", "email", "first_name", "city", "code", "attempts", "expires_at"],
        "fields": [
            {"name": "email",      "label": "Email",      "type": "text", "required": True},
            {"name": "first_name", "label": "Имя",        "type": "text", "required": True},
            {"name": "last_name",  "label": "Фамилия",    "type": "text", "required": True},
            {"name": "phone",      "label": "Телефон",    "type": "text", "required": True},
            {"name": "city",       "label": "Город",      "type": "text", "required": True},
            {"name": "code",       "label": "Код (6 цифр)","type": "text","required": True},
            {"name": "attempts",   "label": "Попытки",    "type": "int",  "required": True},
        ],
    },
}


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Требуется авторизация администратора.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def _parse_admin_pk(pk_str, pk_cols):
    parts = (pk_str or "").split("|")
    if len(parts) != len(pk_cols):
        return None
    try:
        return [int(p) for p in parts]
    except ValueError:
        return None


def _convert_admin_field(field, raw_val, is_edit=False):
    val = (raw_val or "").strip()
    ftype = field["type"]

    if ftype == "password":
        if not val:
            if is_edit:
                return None, True  # пропускаем поле при редактировании, чтобы не стереть хеш
            raise ValueError(f"Поле «{field['label']}» обязательно.")
        if val.startswith(("scrypt:", "pbkdf2:", "demo_hash_")):
            return val, False
        return generate_password_hash(val), False

    if ftype == "bool":
        return val.lower() in ("true", "1", "on", "yes", "да"), False

    if not val:
        if field.get("required"):
            raise ValueError(f"Поле «{field['label']}» обязательно для заполнения.")
        return None, False

    if ftype == "int":
        try:
            return int(val), False
        except ValueError:
            raise ValueError(f"Поле «{field['label']}» должно быть целым числом.")

    if ftype == "decimal":
        dec = parse_price(val)
        if dec is None:
            raise ValueError(f"Поле «{field['label']}» должно быть корректным числом.")
        return dec, False

    return val, False


@app.route("/admin/login", methods=["POST"])
def admin_login():
    password = request.form.get("admin_password", "")
    if secrets.compare_digest(password, ADMIN_PASSWORD):
        session["is_admin"] = True
        flash("Вы вошли в панель администратора базы данных.", "success")
        return redirect(url_for("admin_table", table_name="users"))

    flash("Неверный пароль администратора.", "error")
    return redirect(url_for("login", admin=1))


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("Вы вышли из панели администратора.", "success")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_index():
    return redirect(url_for("admin_table", table_name="users"))


@app.route("/admin/table/<table_name>")
@admin_required
def admin_table(table_name):
    if table_name not in ADMIN_TABLES:
        flash("Таблица не найдена.", "error")
        return redirect(url_for("admin_table", table_name="users"))

    cfg = ADMIN_TABLES[table_name]
    search_q = request.args.get("q", "").strip()
    edit_pk_str = request.args.get("edit", "").strip()
    pk_cols = cfg["pk"]

    rows = []
    edit_values = None

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Загрузка списка записей с опциональной фильтрацией по всем столбцам
            order_sql = pg_sql.SQL(", ").join(
                pg_sql.SQL("{} DESC").format(pg_sql.Identifier(c)) for c in pk_cols
            )
            if search_q:
                where_sql = pg_sql.SQL(" OR ").join(
                    pg_sql.SQL("CAST({} AS TEXT) ILIKE %s").format(pg_sql.Identifier(c))
                    for c in cfg["display_cols"]
                )
                query = pg_sql.SQL("SELECT * FROM {} WHERE {} ORDER BY {}").format(
                    pg_sql.Identifier(table_name), where_sql, order_sql
                )
                cur.execute(query, [f"%{search_q}%"] * len(cfg["display_cols"]))
            else:
                query = pg_sql.SQL("SELECT * FROM {} ORDER BY {}").format(
                    pg_sql.Identifier(table_name), order_sql
                )
                cur.execute(query)

            raw_rows = cur.fetchall()
            for r in raw_rows:
                pk_val = "|".join(str(r[c]) for c in pk_cols)
                rows.append({"pk": pk_val, "data": r})

            # Если нажали «Изменить» — подгружаем редактируемую строку
            if edit_pk_str:
                pk_vals = _parse_admin_pk(edit_pk_str, pk_cols)
                if pk_vals is not None:
                    where_pk = pg_sql.SQL(" AND ").join(
                        pg_sql.SQL("{} = %s").format(pg_sql.Identifier(c)) for c in pk_cols
                    )
                    cur.execute(
                        pg_sql.SQL("SELECT * FROM {} WHERE {}").format(
                            pg_sql.Identifier(table_name), where_pk
                        ),
                        pk_vals,
                    )
                    edit_row = cur.fetchone()
                    if edit_row:
                        edit_values = {}
                        for f in cfg["fields"]:
                            fname = f["name"]
                            raw = edit_row.get(fname)
                            if f["type"] == "password":
                                edit_values[fname] = ""
                            elif f["type"] == "date" and raw is not None:
                                edit_values[fname] = raw.strftime("%Y-%m-%d")
                            elif f["type"] == "decimal" and raw is not None:
                                edit_values[fname] = clean_price_filter(raw)
                            elif f["type"] == "bool":
                                edit_values[fname] = "true" if raw else "false"
                            else:
                                edit_values[fname] = "" if raw is None else str(raw)
    except PsycopgError as err:
        msg = err.diag.message_primary if err.diag and err.diag.message_primary else str(err)
        flash(f"Ошибка БД: {msg}", "error")
    finally:
        conn.close()

    return render_template(
        "admin.html",
        tables=ADMIN_TABLES,
        current_table=table_name,
        cfg=cfg,
        rows=rows,
        search_q=search_q,
        edit_pk_str=edit_pk_str if edit_values is not None else "",
        edit_values=edit_values,
    )


@app.route("/admin/table/<table_name>/add", methods=["POST"])
@admin_required
def admin_add_row(table_name):
    if table_name not in ADMIN_TABLES:
        return redirect(url_for("admin_index"))

    cfg = ADMIN_TABLES[table_name]
    cols, vals = [], []
    try:
        for f in cfg["fields"]:
            converted, skip = _convert_admin_field(f, request.form.get(f["name"]), is_edit=False)
            if not skip:
                cols.append(f["name"])
                vals.append(converted)
    except ValueError as err:
        flash(str(err), "error")
        return redirect(url_for("admin_table", table_name=table_name))

    # Для email_verifications подставим системные значения по умолчанию
    if table_name == "email_verifications":
        cols.extend(["password_hash", "expires_at"])
        vals.extend([generate_password_hash("123456"), datetime.now()])

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            query = pg_sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                pg_sql.Identifier(table_name),
                pg_sql.SQL(", ").join(pg_sql.Identifier(c) for c in cols),
                pg_sql.SQL(", ").join(pg_sql.Placeholder() for _ in cols),
            )
            cur.execute(query, vals)
            conn.commit()
            flash(f"Запись добавлена в таблицу «{table_name}».", "success")
    except PsycopgError as err:
        conn.rollback()
        msg = err.diag.message_primary if err.diag and err.diag.message_primary else str(err)
        flash(f"Ошибка при добавлении: {msg}", "error")
    finally:
        conn.close()

    return redirect(url_for("admin_table", table_name=table_name))


@app.route("/admin/table/<table_name>/edit", methods=["POST"])
@admin_required
def admin_edit_row(table_name):
    if table_name not in ADMIN_TABLES:
        return redirect(url_for("admin_index"))

    cfg = ADMIN_TABLES[table_name]
    pk_cols = cfg["pk"]
    orig_pk = request.form.get("orig_pk", "")
    pk_vals = _parse_admin_pk(orig_pk, pk_cols)
    if pk_vals is None:
        flash("Некорректный идентификатор записи.", "error")
        return redirect(url_for("admin_table", table_name=table_name))

    cols, vals = [], []
    try:
        for f in cfg["fields"]:
            converted, skip = _convert_admin_field(f, request.form.get(f["name"]), is_edit=True)
            if not skip:
                cols.append(f["name"])
                vals.append(converted)
    except ValueError as err:
        flash(str(err), "error")
        return redirect(url_for("admin_table", table_name=table_name, edit=orig_pk))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            set_sql = pg_sql.SQL(", ").join(
                pg_sql.SQL("{} = %s").format(pg_sql.Identifier(c)) for c in cols
            )
            where_sql = pg_sql.SQL(" AND ").join(
                pg_sql.SQL("{} = %s").format(pg_sql.Identifier(c)) for c in pk_cols
            )
            query = pg_sql.SQL("UPDATE {} SET {} WHERE {}").format(
                pg_sql.Identifier(table_name), set_sql, where_sql
            )
            cur.execute(query, vals + pk_vals)
            conn.commit()
            flash(f"Запись обновлена в таблице «{table_name}».", "success")
    except PsycopgError as err:
        conn.rollback()
        msg = err.diag.message_primary if err.diag and err.diag.message_primary else str(err)
        flash(f"Ошибка при обновлении: {msg}", "error")
        return redirect(url_for("admin_table", table_name=table_name, edit=orig_pk))
    finally:
        conn.close()

    return redirect(url_for("admin_table", table_name=table_name))


@app.route("/admin/table/<table_name>/delete", methods=["POST"])
@admin_required
def admin_delete_row(table_name):
    if table_name not in ADMIN_TABLES:
        return redirect(url_for("admin_index"))

    cfg = ADMIN_TABLES[table_name]
    pk_cols = cfg["pk"]
    pk_vals = _parse_admin_pk(request.form.get("pk", ""), pk_cols)
    if pk_vals is None:
        flash("Некорректный идентификатор записи.", "error")
        return redirect(url_for("admin_table", table_name=table_name))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            where_sql = pg_sql.SQL(" AND ").join(
                pg_sql.SQL("{} = %s").format(pg_sql.Identifier(c)) for c in pk_cols
            )
            query = pg_sql.SQL("DELETE FROM {} WHERE {}").format(
                pg_sql.Identifier(table_name), where_sql
            )
            cur.execute(query, pk_vals)
            conn.commit()
            flash(f"Запись удалена из таблицы «{table_name}».", "success")
    except PsycopgError as err:
        conn.rollback()
        msg = err.diag.message_primary if err.diag and err.diag.message_primary else str(err)
        flash(f"Не удалось удалить запись: {msg}", "error")
    finally:
        conn.close()

    return redirect(url_for("admin_table", table_name=table_name))

if __name__ == "__main__":
    app.run(debug=True, port=5000)