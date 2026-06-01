"""
Модуль редактирования метаданных EPUB-файлов.
Поддерживает изменение обложки, названия, автора и других метаданных.
"""

import os
import zipfile
import shutil
import tempfile
import uuid
from pathlib import Path
from lxml import etree
from PIL import Image, ImageDraw, ImageFont
import io


# Пространства имён OPF/Dublin Core
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
XHTML_NS = "http://www.w3.org/1999/xhtml"

NAMESPACES = {
    "opf": OPF_NS,
    "dc": DC_NS,
    "xhtml": XHTML_NS,
}


def _find_opf_path(epub_zip):
    """Находит путь к файлу content.opf внутри epub."""
    with epub_zip.open("META-INF/container.xml") as f:
        tree = etree.parse(f)
    rootfiles = tree.findall(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
    if not rootfiles:
        raise ValueError("Не найден rootfile в container.xml")
    return rootfiles[0].get("full-path")


def read_metadata(epub_path):
    """
    Считывает метаданные из epub-файла.
    Возвращает словарь с ключами: title, author, cover_exists, cover_media_type.
    """
    with zipfile.ZipFile(epub_path, "r") as zf:
        opf_path = _find_opf_path(zf)
        with zf.open(opf_path) as f:
            tree = etree.parse(f)

        root = tree.getroot()
        ns = {"dc": DC_NS, "opf": OPF_NS}

        title_el = root.find(".//dc:title", ns)
        author_el = root.find(".//dc:creator", ns)

        title = title_el.text if title_el is not None else ""
        author = author_el.text if author_el is not None else ""

        # Ищем обложку
        cover_exists = False
        cover_media_type = None
        meta_cover = root.find(".//opf:meta[@name='cover']", ns)
        if meta_cover is not None:
            cover_id = meta_cover.get("content")
            item = root.find(f".//opf:item[@id='{cover_id}']", ns)
            if item is not None:
                cover_media_type = item.get("media-type", "image/jpeg")
                cover_exists = True
        else:
            # Попытка найти через properties="cover-image"
            item = root.find(".//opf:item[@properties='cover-image']", ns)
            if item is not None:
                cover_media_type = item.get("media-type", "image/jpeg")
                cover_exists = True

        return {
            "title": title,
            "author": author,
            "cover_exists": cover_exists,
            "cover_media_type": cover_media_type,
        }


def get_cover_image(epub_path):
    """
    Извлекает обложку из epub.
    Возвращает bytes обложки или None, если обложка не найдена.
    """
    with zipfile.ZipFile(epub_path, "r") as zf:
        opf_path = _find_opf_path(zf)
        opf_dir = str(Path(opf_path).parent)
        with zf.open(opf_path) as f:
            tree = etree.parse(f)
        root = tree.getroot()
        ns = {"dc": DC_NS, "opf": OPF_NS}

        cover_href = None
        meta_cover = root.find(".//opf:meta[@name='cover']", ns)
        if meta_cover is not None:
            cover_id = meta_cover.get("content")
            item = root.find(f".//opf:item[@id='{cover_id}']", ns)
            if item is not None:
                cover_href = item.get("href")
        else:
            item = root.find(".//opf:item[@properties='cover-image']", ns)
            if item is not None:
                cover_href = item.get("href")

        if cover_href is None:
            return None

        # Строим полный путь внутри архива
        if opf_dir and opf_dir != ".":
            full_cover_path = f"{opf_dir}/{cover_href}"
        else:
            full_cover_path = cover_href

        # Нормализуем путь
        full_cover_path = str(Path(full_cover_path))

        try:
            return zf.read(full_cover_path)
        except KeyError:
            # Пробуем без директории
            try:
                return zf.read(cover_href)
            except KeyError:
                return None


def update_metadata(epub_path, output_path, title=None, author=None):
    """
    Обновляет метаданные (название и/или автор) в epub-файле.
    Сохраняет результат в output_path.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Распаковываем epub
        with zipfile.ZipFile(epub_path, "r") as zf:
            zf.extractall(tmpdir)
            opf_path = _find_opf_path(zf)

        opf_full = os.path.join(tmpdir, opf_path)
        tree = etree.parse(opf_full)
        root = tree.getroot()
        ns = {"dc": DC_NS, "opf": OPF_NS}

        if title is not None:
            title_el = root.find(".//dc:title", ns)
            if title_el is not None:
                title_el.text = title
            else:
                metadata = root.find(".//opf:metadata", ns)
                if metadata is not None:
                    new_el = etree.SubElement(metadata, f"{{{DC_NS}}}title")
                    new_el.text = title

        if author is not None:
            author_el = root.find(".//dc:creator", ns)
            if author_el is not None:
                author_el.text = author
            else:
                metadata = root.find(".//opf:metadata", ns)
                if metadata is not None:
                    new_el = etree.SubElement(metadata, f"{{{DC_NS}}}creator")
                    new_el.text = author

        tree.write(opf_full, xml_declaration=True, encoding="utf-8", pretty_print=True)

        # Пересобираем epub
        _pack_epub(tmpdir, output_path)


def process_cover_image(image_bytes, aspect_ratio="2:3", text_lines=None, font_path=None, font_size=48):
    """
    Обрабатывает изображение обложки: обрезает по соотношению сторон,
    опционально добавляет текст.
    
    aspect_ratio: строка вида "2:3", "1:1", "16:9" и т.д.
    text_lines: список словарей {text, color, size, position}
    font_path: путь к TTF-шрифту
    Возвращает bytes обработанного изображения (JPEG).
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    # Парсим соотношение сторон
    ratio_w, ratio_h = map(int, aspect_ratio.split(":"))
    orig_w, orig_h = img.size

    # Вычисляем размер для crop
    if orig_w / orig_h > ratio_w / ratio_h:
        # Исходник шире — обрезаем по ширине
        new_w = int(orig_h * ratio_w / ratio_h)
        new_h = orig_h
    else:
        # Исходник выше — обрезаем по высоте
        new_w = orig_w
        new_h = int(orig_w * ratio_h / ratio_w)

    left = (orig_w - new_w) // 2
    top = (orig_h - new_h) // 2
    img = img.crop((left, top, left + new_w, top + new_h))

    # Масштабируем до стандартного размера обложки
    target_h = 1800
    target_w = int(target_h * ratio_w / ratio_h)
    img = img.resize((target_w, target_h), Image.LANCZOS)

    # Добавляем текст
    if text_lines:
        draw = ImageDraw.Draw(img)
        for line in text_lines:
            text = line.get("text", "")
            if not text:
                continue
            color = line.get("color", "#FFFFFF")
            size = int(line.get("size", font_size))
            # Поддержка процентных координат (x_pct/y_pct) и абсолютных (x/y)
            if "x_pct" in line:
                x = int(float(line["x_pct"]) * img.width)
            else:
                x = int(line.get("x", img.width // 2))
            if "y_pct" in line:
                y = int(float(line["y_pct"]) * img.height)
            else:
                y = int(line.get("y", img.height // 2))
            shadow = line.get("shadow", True)

            # Загружаем шрифт
            font = _load_font(font_path or line.get("font_path"), size)

            # Тень для читаемости
            if shadow:
                for dx, dy in [(-2, -2), (2, -2), (-2, 2), (2, 2)]:
                    draw.text((x + dx, y + dy), text, font=font, fill="#000000AA", anchor="mm")

            draw.text((x, y), text, font=font, fill=color, anchor="mm")

    # Сохраняем в bytes
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()


def set_cover(epub_path, output_path, cover_bytes, media_type="image/jpeg"):
    """
    Устанавливает новую обложку в epub-файл.
    cover_bytes: bytes изображения
    """
    ext_map = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    ext = ext_map.get(media_type, "jpg")

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(epub_path, "r") as zf:
            zf.extractall(tmpdir)
            opf_path = _find_opf_path(zf)

        opf_dir = str(Path(opf_path).parent)
        opf_full = os.path.join(tmpdir, opf_path)

        tree = etree.parse(opf_full)
        root = tree.getroot()
        ns = {"dc": DC_NS, "opf": OPF_NS}

        # Ищем существующую обложку
        cover_href = None
        cover_id = "cover-image"
        meta_cover = root.find(".//opf:meta[@name='cover']", ns)
        manifest = root.find(".//opf:manifest", ns)

        if meta_cover is not None:
            cid = meta_cover.get("content")
            item = root.find(f".//opf:item[@id='{cid}']", ns)
            if item is not None:
                cover_href = item.get("href")
                cover_id = cid
        else:
            item = root.find(".//opf:item[@properties='cover-image']", ns)
            if item is not None:
                cover_href = item.get("href")
                cover_id = item.get("id", "cover-image")

        # Определяем путь для новой обложки
        new_cover_filename = f"cover.{ext}"
        if cover_href:
            # Используем тот же путь, меняем расширение
            new_cover_filename = str(Path(cover_href).with_suffix(f".{ext}"))
        new_cover_href = new_cover_filename

        # Сохраняем файл обложки
        if opf_dir and opf_dir != ".":
            cover_disk_path = os.path.join(tmpdir, opf_dir, new_cover_filename)
        else:
            cover_disk_path = os.path.join(tmpdir, new_cover_filename)

        os.makedirs(os.path.dirname(cover_disk_path), exist_ok=True)
        with open(cover_disk_path, "wb") as f:
            f.write(cover_bytes)

        # Обновляем манифест
        existing_item = root.find(f".//opf:item[@id='{cover_id}']", ns)
        if existing_item is not None:
            existing_item.set("href", new_cover_href)
            existing_item.set("media-type", media_type)
            # Убираем старый файл если имя изменилось
        else:
            # Создаём новый элемент в манифесте
            new_item = etree.SubElement(manifest, f"{{{OPF_NS}}}item")
            new_item.set("id", cover_id)
            new_item.set("href", new_cover_href)
            new_item.set("media-type", media_type)
            new_item.set("properties", "cover-image")

        # Добавляем/обновляем meta cover для epub2
        existing_meta = root.find(".//opf:meta[@name='cover']", ns)
        if existing_meta is not None:
            existing_meta.set("content", cover_id)
        else:
            metadata = root.find(".//opf:metadata", ns)
            if metadata is not None:
                meta_el = etree.SubElement(metadata, f"{{{OPF_NS}}}meta")
                meta_el.set("name", "cover")
                meta_el.set("content", cover_id)

        tree.write(opf_full, xml_declaration=True, encoding="utf-8", pretty_print=True)
        _pack_epub(tmpdir, output_path)


def _load_font(font_path, size):
    """Загружает шрифт по пути или возвращает дефолтный."""
    if font_path and os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    # Пробуем системные шрифты с кириллицей
    fallbacks = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for fb in fallbacks:
        if os.path.exists(fb):
            try:
                return ImageFont.truetype(fb, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _pack_epub(source_dir, output_path):
    """
    Упаковывает директорию в epub-файл.
    mimetype должен быть первым и без сжатия.
    """
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        mimetype_path = os.path.join(source_dir, "mimetype")
        if os.path.exists(mimetype_path):
            zf.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)

        for root_dir, dirs, files in os.walk(source_dir):
            # Пропускаем скрытые директории
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                file_path = os.path.join(root_dir, file)
                arcname = os.path.relpath(file_path, source_dir)
                if arcname == "mimetype":
                    continue
                zf.write(file_path, arcname)
