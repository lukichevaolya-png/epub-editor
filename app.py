"""
Основной файл Flask-приложения для редактора EPUB.
Запуск: python app.py
"""

import os
import sys
import uuid
import json
import tempfile
import shutil
import mimetypes
from pathlib import Path
from flask import (
    Flask, request, jsonify, send_file, render_template,
    send_from_directory, after_this_request
)
from werkzeug.utils import secure_filename

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(__file__))

from core.epub_editor import (
    read_metadata, get_cover_image, update_metadata,
    process_cover_image, set_cover
)
from core.epub_merger import merge_epubs


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB максимальный размер файла

# Директории для временных файлов
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "epub_editor_uploads")
OUTPUT_DIR = os.path.join(tempfile.gettempdir(), "epub_editor_outputs")
FONTS_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(FONTS_DIR, exist_ok=True)


ALLOWED_EPUB = {"epub"}
ALLOWED_IMAGE = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff"}
ALLOWED_FONT = {"ttf", "otf", "woff", "woff2"}


def allowed_file(filename, allowed_set):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_set


def get_temp_path(filename):
    """Генерирует уникальный временный путь для загруженного файла."""
    unique = str(uuid.uuid4())
    return os.path.join(UPLOAD_DIR, f"{unique}_{secure_filename(filename)}")


def get_output_path(filename):
    """Генерирует путь для выходного файла."""
    unique = str(uuid.uuid4())
    return os.path.join(OUTPUT_DIR, f"{unique}_{secure_filename(filename)}")


# ─── Маршруты ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Главная страница."""
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload_epub():
    """
    Загружает epub-файл и возвращает его метаданные.
    """
    if "file" not in request.files:
        return jsonify({"error": "Файл не найден в запросе"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Имя файла пустое"}), 400

    if not allowed_file(file.filename, ALLOWED_EPUB):
        return jsonify({"error": "Допустимы только файлы .epub"}), 400

    temp_path = get_temp_path(file.filename)
    file.save(temp_path)

    try:
        metadata = read_metadata(temp_path)
        cover_bytes = get_cover_image(temp_path)

        return jsonify({
            "file_id": os.path.basename(temp_path),
            "original_name": file.filename,
            "metadata": metadata,
            "has_cover": cover_bytes is not None,
        })
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return jsonify({"error": f"Ошибка чтения epub: {str(e)}"}), 500


@app.route("/api/cover/<file_id>")
def get_cover(file_id):
    """Возвращает обложку epub как изображение."""
    # Защита от path traversal
    safe_id = secure_filename(file_id)
    epub_path = os.path.join(UPLOAD_DIR, safe_id)

    if not os.path.exists(epub_path):
        return jsonify({"error": "Файл не найден"}), 404

    try:
        cover_bytes = get_cover_image(epub_path)
        if cover_bytes is None:
            return jsonify({"error": "Обложка не найдена"}), 404

        # Определяем тип изображения
        import imghdr
        img_type = imghdr.what(None, h=cover_bytes) or "jpeg"
        mime = f"image/{img_type}"

        import io
        return send_file(io.BytesIO(cover_bytes), mimetype=mime)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/process-cover", methods=["POST"])
def process_cover():
    """
    Обрабатывает изображение для обложки: обрезка и добавление текста.
    Возвращает обработанное изображение.
    """
    if "image" not in request.files:
        return jsonify({"error": "Изображение не найдено"}), 400

    img_file = request.files["image"]
    if not allowed_file(img_file.filename, ALLOWED_IMAGE):
        return jsonify({"error": "Недопустимый формат изображения"}), 400

    aspect_ratio = request.form.get("aspect_ratio", "2:3")
    text_lines_raw = request.form.get("text_lines", "[]")
    font_id = request.form.get("font_id", "")

    try:
        text_lines = json.loads(text_lines_raw)
    except Exception:
        text_lines = []

    # Путь к выбранному шрифту
    font_path = None
    if font_id:
        candidate = os.path.join(FONTS_DIR, secure_filename(font_id))
        if os.path.exists(candidate):
            font_path = candidate

    try:
        image_bytes = img_file.read()
        result_bytes = process_cover_image(
            image_bytes,
            aspect_ratio=aspect_ratio,
            text_lines=text_lines,
            font_path=font_path,
        )

        import io
        return send_file(io.BytesIO(result_bytes), mimetype="image/jpeg")
    except Exception as e:
        return jsonify({"error": f"Ошибка обработки изображения: {str(e)}"}), 500


@app.route("/api/save", methods=["POST"])
def save_epub():
    """
    Применяет изменения к epub и возвращает готовый файл.
    Принимает multipart/form-data с полями:
    - file_id: ID загруженного epub
    - title: новое название
    - author: новый автор
    - cover: (опционально) новое изображение обложки
    - aspect_ratio: соотношение сторон
    - text_lines: JSON-массив текстовых блоков
    - font_id: ID шрифта
    """
    file_id = request.form.get("file_id", "")
    if not file_id:
        return jsonify({"error": "Не указан file_id"}), 400

    safe_id = secure_filename(file_id)
    epub_path = os.path.join(UPLOAD_DIR, safe_id)

    if not os.path.exists(epub_path):
        return jsonify({"error": "Файл не найден. Загрузите epub заново"}), 404

    title = request.form.get("title")
    author = request.form.get("author")
    aspect_ratio = request.form.get("aspect_ratio", "2:3")
    text_lines_raw = request.form.get("text_lines", "[]")
    font_id = request.form.get("font_id", "")

    try:
        text_lines = json.loads(text_lines_raw)
    except Exception:
        text_lines = []

    font_path = None
    if font_id:
        candidate = os.path.join(FONTS_DIR, secure_filename(font_id))
        if os.path.exists(candidate):
            font_path = candidate

    # Генерируем имя выходного файла
    original_name = safe_id.split("_", 1)[-1] if "_" in safe_id else safe_id
    output_path = get_output_path(original_name)

    try:
        current_path = epub_path

        # Шаг 1: обновляем метаданные
        if title is not None or author is not None:
            meta_output = output_path + ".meta.epub"
            update_metadata(current_path, meta_output, title=title or None, author=author or None)
            current_path = meta_output

        # Шаг 2: устанавливаем обложку
        if "cover" in request.files:
            cover_file = request.files["cover"]
            if cover_file.filename and allowed_file(cover_file.filename, ALLOWED_IMAGE):
                cover_bytes = cover_file.read()

                # Определяем MIME
                ext = cover_file.filename.rsplit(".", 1)[-1].lower()
                mime_map = {
                    "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "webp": "image/webp",
                    "gif": "image/gif",
                }
                media_type = mime_map.get(ext, "image/jpeg")

                # Обрабатываем изображение
                processed = process_cover_image(
                    cover_bytes,
                    aspect_ratio=aspect_ratio,
                    text_lines=text_lines,
                    font_path=font_path,
                )

                cover_output = output_path + ".cover.epub"
                set_cover(current_path, cover_output, processed, media_type="image/jpeg")
                if current_path != epub_path:
                    os.remove(current_path)
                current_path = cover_output

        # Если ничего не изменилось — копируем оригинал
        if current_path == epub_path:
            shutil.copy2(epub_path, output_path)
        elif current_path != output_path:
            shutil.move(current_path, output_path)

        # Отправляем файл
        download_name = f"edited_{original_name}"

        @after_this_request
        def cleanup(response):
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
            return response

        return send_file(
            output_path,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/epub+zip",
        )

    except Exception as e:
        for p in [output_path, output_path + ".meta.epub", output_path + ".cover.epub"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        return jsonify({"error": f"Ошибка сохранения: {str(e)}"}), 500


@app.route("/api/fonts", methods=["GET"])
def list_fonts():
    """Возвращает список доступных шрифтов."""
    fonts = []
    if os.path.exists(FONTS_DIR):
        for f in sorted(os.listdir(FONTS_DIR)):
            if f.lower().endswith((".ttf", ".otf")):
                fonts.append({"id": f, "name": Path(f).stem.replace("_", " ").replace("-", " ")})
    return jsonify({"fonts": fonts})


@app.route("/api/fonts/upload", methods=["POST"])
def upload_font():
    """Загружает пользовательский шрифт."""
    if "font" not in request.files:
        return jsonify({"error": "Шрифт не найден"}), 400

    font_file = request.files["font"]
    if not allowed_file(font_file.filename, ALLOWED_FONT):
        return jsonify({"error": "Допустимы только TTF, OTF, WOFF, WOFF2"}), 400

    filename = secure_filename(font_file.filename)
    save_path = os.path.join(FONTS_DIR, filename)
    font_file.save(save_path)

    return jsonify({
        "id": filename,
        "name": Path(filename).stem.replace("_", " ").replace("-", " "),
    })


@app.route("/api/merge/upload", methods=["POST"])
def merge_upload():
    """
    Загружает несколько epub для объединения.
    Возвращает список file_id с метаданными.
    """
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Файлы не загружены"}), 400

    uploaded = []
    for file in files:
        if not file.filename or not allowed_file(file.filename, ALLOWED_EPUB):
            continue
        temp_path = get_temp_path(file.filename)
        file.save(temp_path)
        try:
            metadata = read_metadata(temp_path)
            uploaded.append({
                "file_id": os.path.basename(temp_path),
                "original_name": file.filename,
                "metadata": metadata,
            })
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    if not uploaded:
        return jsonify({"error": "Ни один файл не был успешно загружен"}), 400

    return jsonify({"files": uploaded})


@app.route("/api/merge/execute", methods=["POST"])
def merge_execute():
    """
    Выполняет объединение epub-файлов.
    Принимает JSON:
    {
        "order": ["file_id_1", "file_id_2", ...],
        "title": "Название",
        "author": "Автор",
        "output_name": "result.epub"
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Нет данных"}), 400

    order = data.get("order", [])
    merged_title = data.get("title", "Объединённая книга")
    merged_author = data.get("author", "")
    output_name = data.get("output_name", "merged.epub")

    if len(order) < 2:
        return jsonify({"error": "Нужно минимум 2 файла для объединения"}), 400

    epub_paths = []
    for file_id in order:
        safe_id = secure_filename(file_id)
        epub_path = os.path.join(UPLOAD_DIR, safe_id)
        if not os.path.exists(epub_path):
            return jsonify({"error": f"Файл не найден: {file_id}"}), 404
        epub_paths.append(epub_path)

    output_path = get_output_path(secure_filename(output_name))

    try:
        merge_epubs(
            epub_paths,
            output_path,
            merged_title=merged_title,
            merged_author=merged_author,
        )

        @after_this_request
        def cleanup(response):
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
            return response

        return send_file(
            output_path,
            as_attachment=True,
            download_name=output_name,
            mimetype="application/epub+zip",
        )

    except Exception as e:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        return jsonify({"error": f"Ошибка объединения: {str(e)}"}), 500


@app.route("/api/cleanup", methods=["POST"])
def cleanup_files():
    """Удаляет временные файлы по списку file_id."""
    data = request.get_json() or {}
    file_ids = data.get("file_ids", [])

    for file_id in file_ids:
        safe_id = secure_filename(file_id)
        path = os.path.join(UPLOAD_DIR, safe_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    return jsonify({"ok": True})


if __name__ == "__main__":
    print("=" * 60)
    print("  EPUB Editor — локальный сервер")
    print("  Откройте в браузере: http://localhost:5000")
    print("=" * 60)
    app.run(debug=True, host="127.0.0.1", port=5000)
