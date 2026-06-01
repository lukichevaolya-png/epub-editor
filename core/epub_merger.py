"""
Модуль объединения нескольких EPUB-файлов в один.
Объединяет содержимое, оглавление, метаданные и ресурсы.
"""

import os
import zipfile
import shutil
import tempfile
import re
from pathlib import Path
from lxml import etree
from copy import deepcopy


OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
XHTML_NS = "http://www.w3.org/1999/xhtml"
OPS_NS = "http://www.idpf.org/2007/ops"


def _find_opf_path(epub_zip):
    with epub_zip.open("META-INF/container.xml") as f:
        tree = etree.parse(f)
    rootfiles = tree.findall(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
    if not rootfiles:
        raise ValueError("Не найден rootfile в container.xml")
    return rootfiles[0].get("full-path")


def _sanitize_id(s):
    """Превращает строку в валидный XML ID."""
    s = re.sub(r"[^a-zA-Z0-9_\-]", "_", s)
    if s and s[0].isdigit():
        s = "id_" + s
    return s or "item"


def _extract_epub(epub_path, target_dir):
    """Распаковывает epub и возвращает путь к OPF и дерево OPF."""
    with zipfile.ZipFile(epub_path, "r") as zf:
        zf.extractall(target_dir)
        opf_path = _find_opf_path(zf)
    opf_full = os.path.join(target_dir, opf_path)
    tree = etree.parse(opf_full)
    return opf_path, tree


def _get_opf_dir(opf_path):
    d = str(Path(opf_path).parent)
    return "" if d == "." else d


def merge_epubs(epub_paths, output_path, merged_title="Объединённая книга", merged_author=""):
    """
    Объединяет список epub-файлов в один.
    
    epub_paths: список путей к epub в нужном порядке
    output_path: путь для сохранения результата
    merged_title: название итоговой книги
    merged_author: автор итоговой книги
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = os.path.join(tmpdir, "merged")
        os.makedirs(out_dir)
        os.makedirs(os.path.join(out_dir, "META-INF"))
        os.makedirs(os.path.join(out_dir, "OEBPS"))
        os.makedirs(os.path.join(out_dir, "OEBPS", "images"))
        os.makedirs(os.path.join(out_dir, "OEBPS", "fonts"))
        os.makedirs(os.path.join(out_dir, "OEBPS", "css"))
        os.makedirs(os.path.join(out_dir, "OEBPS", "chapters"))

        # Записываем mimetype
        with open(os.path.join(out_dir, "mimetype"), "w", encoding="utf-8") as f:
            f.write("application/epub+zip")

        # Записываем container.xml
        container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
        with open(os.path.join(out_dir, "META-INF", "container.xml"), "w", encoding="utf-8") as f:
            f.write(container_xml)

        # Структуры для сборки итогового OPF и NCX
        all_manifest_items = {}  # id -> {href, media-type}
        all_spine_items = []     # список href в порядке чтения
        all_toc_items = []       # список {label, href, children}
        css_collected = {}       # original_name -> new_name
        fonts_collected = {}
        images_collected = {}
        chapter_counter = [0]
        css_counter = [0]
        font_counter = [0]
        image_counter = [0]

        def unique_chapter_id():
            chapter_counter[0] += 1
            return f"chapter_{chapter_counter[0]:04d}"

        def unique_image_name(orig_name):
            ext = Path(orig_name).suffix
            image_counter[0] += 1
            return f"img_{image_counter[0]:04d}{ext}"

        def unique_css_name(orig_name):
            ext = Path(orig_name).suffix
            css_counter[0] += 1
            return f"style_{css_counter[0]:04d}{ext}"

        def unique_font_name(orig_name):
            ext = Path(orig_name).suffix
            font_counter[0] += 1
            return f"font_{font_counter[0]:04d}{ext}"

        # Обрабатываем каждый epub
        for book_idx, epub_path in enumerate(epub_paths):
            book_dir = os.path.join(tmpdir, f"book_{book_idx}")
            os.makedirs(book_dir)

            with zipfile.ZipFile(epub_path, "r") as zf:
                zf.extractall(book_dir)
                opf_path = _find_opf_path(zf)

            opf_dir = _get_opf_dir(opf_path)
            opf_full = os.path.join(book_dir, opf_path)

            tree = etree.parse(opf_full)
            root = tree.getroot()
            ns = {"dc": DC_NS, "opf": OPF_NS}

            # Собираем manifest items этой книги
            manifest = root.find(".//opf:manifest", ns)
            spine = root.find(".//opf:spine", ns)

            if manifest is None or spine is None:
                continue

            # Маппинг старый id -> новый href для этой книги
            item_map = {}  # old_id -> new_href_relative_to_OEBPS

            for item in manifest.findall("opf:item", ns):
                item_id = item.get("id", "")
                item_href = item.get("href", "")
                media_type = item.get("media-type", "")
                properties = item.get("properties", "")

                # Полный путь на диске
                if opf_dir:
                    disk_path = os.path.join(book_dir, opf_dir, item_href)
                else:
                    disk_path = os.path.join(book_dir, item_href)

                if not os.path.exists(disk_path):
                    continue

                # Пропускаем NCX и nav — пересоздадим сами
                if media_type == "application/x-dtbncx+xml":
                    continue
                if "nav" in properties:
                    continue

                if media_type in ("text/html", "application/xhtml+xml"):
                    # Глава
                    new_id = unique_chapter_id()
                    new_filename = f"{new_id}.xhtml"
                    new_href = f"chapters/{new_filename}"

                    # Читаем содержимое главы
                    with open(disk_path, "rb") as f:
                        content = f.read()

                    # Фиксируем относительные ссылки на ресурсы
                    content_str = content.decode("utf-8", errors="replace")

                    item_map[item_id] = new_href
                    all_manifest_items[new_id] = {
                        "href": new_href,
                        "media-type": "application/xhtml+xml",
                    }
                    # Временно сохраняем для последующей обработки ссылок
                    tmp_path = os.path.join(out_dir, "OEBPS", new_href)
                    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
                    with open(tmp_path, "wb") as f:
                        f.write(content_str.encode("utf-8"))

                elif media_type.startswith("image/"):
                    # Изображение
                    orig_name = Path(item_href).name
                    cache_key = f"{book_idx}:{item_href}"
                    if cache_key not in images_collected:
                        new_name = unique_image_name(orig_name)
                        images_collected[cache_key] = new_name
                        dst = os.path.join(out_dir, "OEBPS", "images", new_name)
                        shutil.copy2(disk_path, dst)
                    new_name = images_collected[cache_key]
                    new_href = f"images/{new_name}"
                    img_id = f"img_{_sanitize_id(new_name)}"
                    all_manifest_items[img_id] = {"href": new_href, "media-type": media_type}
                    if "cover" in properties or "cover" in item_id.lower():
                        all_manifest_items[img_id]["properties"] = "cover-image"
                    item_map[item_id] = new_href

                elif media_type in ("text/css", "text/x-css"):
                    # CSS
                    orig_name = Path(item_href).name
                    cache_key = f"{book_idx}:{item_href}"
                    if cache_key not in css_collected:
                        new_name = unique_css_name(orig_name)
                        css_collected[cache_key] = new_name
                        dst = os.path.join(out_dir, "OEBPS", "css", new_name)
                        shutil.copy2(disk_path, dst)
                    new_name = css_collected[cache_key]
                    new_href = f"css/{new_name}"
                    css_id = f"css_{_sanitize_id(new_name)}"
                    all_manifest_items[css_id] = {"href": new_href, "media-type": "text/css"}
                    item_map[item_id] = new_href

                elif media_type in (
                    "application/font-sfnt", "application/vnd.ms-opentype",
                    "font/otf", "font/ttf", "application/font-woff",
                    "font/woff", "font/woff2",
                ):
                    # Шрифт
                    orig_name = Path(item_href).name
                    cache_key = f"{book_idx}:{item_href}"
                    if cache_key not in fonts_collected:
                        new_name = unique_font_name(orig_name)
                        fonts_collected[cache_key] = new_name
                        dst = os.path.join(out_dir, "OEBPS", "fonts", new_name)
                        shutil.copy2(disk_path, dst)
                    new_name = fonts_collected[cache_key]
                    new_href = f"fonts/{new_name}"
                    font_id = f"font_{_sanitize_id(new_name)}"
                    all_manifest_items[font_id] = {"href": new_href, "media-type": media_type}
                    item_map[item_id] = new_href

            # Собираем spine в правильном порядке
            toc_ncx_id = spine.get("toc", "")
            for itemref in spine.findall("opf:itemref", ns):
                ref_id = itemref.get("idref", "")
                if ref_id in item_map:
                    new_href = item_map[ref_id]
                    if new_href.startswith("chapters/"):
                        all_spine_items.append(new_href)

            # Собираем оглавление
            book_title_el = root.find(".//dc:title", ns)
            book_title = book_title_el.text if book_title_el is not None else f"Книга {book_idx + 1}"

            # Пробуем прочитать NCX
            ncx_item = root.find(f".//opf:item[@id='{toc_ncx_id}']", ns) if toc_ncx_id else None
            book_toc = []

            if ncx_item is not None:
                ncx_href = ncx_item.get("href", "")
                if opf_dir:
                    ncx_disk = os.path.join(book_dir, opf_dir, ncx_href)
                else:
                    ncx_disk = os.path.join(book_dir, ncx_href)

                if os.path.exists(ncx_disk):
                    try:
                        ncx_tree = etree.parse(ncx_disk)
                        ncx_root = ncx_tree.getroot()
                        nav_map = ncx_root.find(f"{{{NCX_NS}}}navMap")
                        if nav_map is not None:
                            for nav_point in nav_map.findall(f"{{{NCX_NS}}}navPoint"):
                                label_el = nav_point.find(f".//{{{NCX_NS}}}text")
                                content_el = nav_point.find(f"{{{NCX_NS}}}content")
                                if label_el is not None and content_el is not None:
                                    label = label_el.text or ""
                                    src = content_el.get("src", "")
                                    # Убираем якорь для поиска файла
                                    src_file = src.split("#")[0]
                                    anchor = src.split("#")[1] if "#" in src else ""
                                    # Ищем соответствующий новый href
                                    for old_id, new_href in item_map.items():
                                        item_el = root.find(f".//opf:item[@id='{old_id}']", ns)
                                        if item_el is not None:
                                            old_href = item_el.get("href", "")
                                            if old_href == src_file or Path(old_href).name == Path(src_file).name:
                                                new_src = f"../chapters/{Path(new_href).name}"
                                                if anchor:
                                                    new_src += f"#{anchor}"
                                                book_toc.append({"label": label, "href": new_src})
                                                break
                    except Exception:
                        pass

            # Если NCX не дал результатов — добавляем главы без названий
            if not book_toc:
                for old_id, new_href in item_map.items():
                    if new_href.startswith("chapters/"):
                        book_toc.append({
                            "label": Path(new_href).stem,
                            "href": f"../chapters/{Path(new_href).name}"
                        })

            all_toc_items.append({
                "label": book_title,
                "href": book_toc[0]["href"] if book_toc else "",
                "children": book_toc,
            })

        # Теперь обновляем ссылки в xhtml-главах
        _fix_chapter_links(
            os.path.join(out_dir, "OEBPS", "chapters"),
            all_manifest_items,
        )

        # Создаём NCX
        _write_ncx(
            os.path.join(out_dir, "OEBPS", "toc.ncx"),
            merged_title,
            merged_author,
            all_toc_items,
        )

        # Создаём nav.xhtml (epub3)
        _write_nav(
            os.path.join(out_dir, "OEBPS", "nav.xhtml"),
            merged_title,
            all_toc_items,
        )

        # Добавляем NCX и nav в manifest
        all_manifest_items["ncx"] = {
            "href": "toc.ncx",
            "media-type": "application/x-dtbncx+xml",
        }
        all_manifest_items["nav"] = {
            "href": "nav.xhtml",
            "media-type": "application/xhtml+xml",
            "properties": "nav",
        }

        # Создаём content.opf
        _write_opf(
            os.path.join(out_dir, "OEBPS", "content.opf"),
            merged_title,
            merged_author,
            all_manifest_items,
            all_spine_items,
        )

        # Упаковываем
        _pack_epub(out_dir, output_path)


def _fix_chapter_links(chapters_dir, manifest_items):
    """
    Исправляет относительные ссылки внутри xhtml-файлов глав.
    Заменяет старые пути к ресурсам на новые.
    """
    if not os.path.exists(chapters_dir):
        return

    for filename in os.listdir(chapters_dir):
        if not filename.endswith((".xhtml", ".html")):
            continue
        filepath = os.path.join(chapters_dir, filename)
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Ищем ссылки на изображения и заменяем на ../images/...
        def replace_src(m):
            attr = m.group(1)
            val = m.group(2)
            # Оставляем абсолютные ссылки как есть
            if val.startswith(("http://", "https://", "data:")):
                return m.group(0)
            # Нормализуем путь
            name = Path(val).name
            # Ищем в manifest
            for item_id, item in manifest_items.items():
                if Path(item["href"]).name == name:
                    rel_path = f"../{item['href']}"
                    return f'{attr}="{rel_path}"'
            return m.group(0)

        content = re.sub(r'(src)="([^"]+)"', replace_src, content)
        content = re.sub(r"(src)='([^']+)'", replace_src, content)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)


def _write_opf(opf_path, title, author, manifest_items, spine_items):
    """Записывает content.opf итогового файла."""
    import uuid as _uuid
    book_id = str(_uuid.uuid4())

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="BookId">',
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">',
        f'    <dc:identifier id="BookId">{book_id}</dc:identifier>',
        f'    <dc:title>{_xml_escape(title)}</dc:title>',
        f'    <dc:creator>{_xml_escape(author)}</dc:creator>',
        '    <dc:language>ru</dc:language>',
        '    <meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>',
        '  </metadata>',
        '  <manifest>',
    ]

    for item_id, item in manifest_items.items():
        props = item.get("properties", "")
        props_attr = f' properties="{props}"' if props else ""
        lines.append(
            f'    <item id="{_xml_escape(item_id)}" '
            f'href="{_xml_escape(item["href"])}" '
            f'media-type="{_xml_escape(item["media-type"])}"'
            f'{props_attr}/>'
        )

    lines.append('  </manifest>')
    lines.append('  <spine toc="ncx">')

    for href in spine_items:
        # Находим id по href
        item_id = None
        for iid, item in manifest_items.items():
            if item["href"] == href:
                item_id = iid
                break
        if item_id:
            lines.append(f'    <itemref idref="{_xml_escape(item_id)}"/>')

    lines.append('  </spine>')
    lines.append('</package>')

    with open(opf_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_ncx(ncx_path, title, author, toc_items):
    """Записывает toc.ncx для epub2-совместимости."""
    play_order = [0]

    def po():
        play_order[0] += 1
        return play_order[0]

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">',
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">',
        '  <head>',
        '    <meta name="dtb:uid" content="book-id"/>',
        '    <meta name="dtb:depth" content="2"/>',
        '  </head>',
        f'  <docTitle><text>{_xml_escape(title)}</text></docTitle>',
        '  <navMap>',
    ]

    for section in toc_items:
        order = po()
        lines.append(f'    <navPoint id="navpoint-{order}" playOrder="{order}">')
        lines.append(f'      <navLabel><text>{_xml_escape(section["label"])}</text></navLabel>')
        lines.append(f'      <content src="{_xml_escape(section.get("href", ""))}"/>')

        for child in section.get("children", []):
            child_order = po()
            lines.append(f'      <navPoint id="navpoint-{child_order}" playOrder="{child_order}">')
            lines.append(f'        <navLabel><text>{_xml_escape(child["label"])}</text></navLabel>')
            lines.append(f'        <content src="{_xml_escape(child["href"])}"/>')
            lines.append('      </navPoint>')

        lines.append('    </navPoint>')

    lines += ['  </navMap>', '</ncx>']

    with open(ncx_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_nav(nav_path, title, toc_items):
    """Записывает nav.xhtml для epub3."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE html>',
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ru">',
        '<head><meta charset="utf-8"/><title>Оглавление</title></head>',
        '<body>',
        '  <nav epub:type="toc" id="toc">',
        f'    <h1>{_xml_escape(title)}</h1>',
        '    <ol>',
    ]

    for section in toc_items:
        href = section.get("href", "")
        label = section["label"]
        children = section.get("children", [])
        if href:
            lines.append(f'      <li><a href="{_xml_escape(href)}">{_xml_escape(label)}</a>')
        else:
            lines.append(f'      <li><span>{_xml_escape(label)}</span>')

        if children:
            lines.append('        <ol>')
            for child in children:
                lines.append(
                    f'          <li><a href="{_xml_escape(child["href"])}">'
                    f'{_xml_escape(child["label"])}</a></li>'
                )
            lines.append('        </ol>')
        lines.append('      </li>')

    lines += ['    </ol>', '  </nav>', '</body>', '</html>']

    with open(nav_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _xml_escape(s):
    if not s:
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _pack_epub(source_dir, output_path):
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        mimetype_path = os.path.join(source_dir, "mimetype")
        if os.path.exists(mimetype_path):
            zf.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
        for root_dir, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                file_path = os.path.join(root_dir, file)
                arcname = os.path.relpath(file_path, source_dir)
                if arcname == "mimetype":
                    continue
                zf.write(file_path, arcname)
