import sys
import time
import smtplib
import json
import os
from email.mime.text import MIMEText
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from datetime import datetime
import subprocess

# --- CONFIGURACIÓN ---
# Las credenciales se leen de variables de entorno para no dejarlas escritas
# en el código. En local puedes exportarlas en la terminal o usar un .env;
# en GitHub Actions vienen de los Secrets del repositorio.
ARCHIVO_PRODUCTOS = "productos.json"  # Lista de productos a monitorear
TU_EMAIL = os.environ.get("ZARA_EMAIL", "")
TU_PASSWORD_APP = os.environ.get("ZARA_EMAIL_PASSWORD", "")  # Contraseña de aplicación de Gmail
EMAIL_DESTINO = os.environ.get("ZARA_EMAIL_DESTINO", "") or TU_EMAIL
INTERVALO_MINUTOS = int(os.environ.get("ZARA_INTERVALO_MINUTOS", "10"))
ARCHIVO_ESTADO = "estado_productos.json"  # Archivo para guardar el estado

# Sin pantalla (servidores, GitHub Actions) no puede abrirse una ventana real.
SIN_VENTANA = os.environ.get("ZARA_HEADLESS", "").lower() in ("1", "true", "si", "yes") or bool(os.environ.get("CI"))


def _cargar_env_local():
    """Lee un archivo .env si existe, para no tener que exportar nada a mano."""
    if not os.path.exists(".env"):
        return
    with open(".env", encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


_cargar_env_local()
TU_EMAIL = TU_EMAIL or os.environ.get("ZARA_EMAIL", "")
TU_PASSWORD_APP = TU_PASSWORD_APP or os.environ.get("ZARA_EMAIL_PASSWORD", "")
EMAIL_DESTINO = EMAIL_DESTINO or os.environ.get("ZARA_EMAIL_DESTINO", "") or TU_EMAIL


def construir_opciones():
    """Opciones de Chrome, distintas en local y en servidor."""
    options = Options()
    if SIN_VENTANA:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1280,900")
    else:
        # Ventana pequeña: evita bloqueos de Zara sin molestar demasiado
        options.add_argument("--window-size=400,300")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return options


def crear_driver():
    """Crea el driver de Chrome.

    En local usa webdriver_manager; en servidor deja que Selenium Manager
    resuelva el chromedriver que ya viene instalado.
    """
    options = construir_opciones()
    try:
        return webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=options
        )
    except Exception as e:
        print(f"⚠️ webdriver_manager falló ({e}); probando con Selenium Manager...")
        return webdriver.Chrome(options=options)

def notificacion_sistema(titulo, mensaje):
    """Envía notificación del sistema (solo macOS; en servidor se omite)"""
    if sys.platform != "darwin":
        return
    try:
        script = f'display notification "{mensaje}" with title "{titulo}" sound name "Glass"'
        subprocess.run(['osascript', '-e', script])
        print(f"🔔 Notificación enviada: {titulo}")
    except Exception as e:
        print(f"❌ Error en notificación: {e}")

def cargar_productos():
    """Carga la lista de productos desde el archivo JSON"""
    try:
        with open(ARCHIVO_PRODUCTOS, 'r', encoding='utf-8') as f:
            productos = json.load(f)
        # Filtrar solo productos activos
        return [p for p in productos if p.get('activo', True)]
    except FileNotFoundError:
        print(f"❌ No se encontró {ARCHIVO_PRODUCTOS}")
        return []
    except Exception as e:
        print(f"❌ Error cargando productos: {e}")
        return []

def enviar_correo(talla, producto_nombre, producto_url):
    print("📧 Intentando enviar correo...")
    if not TU_EMAIL or not TU_PASSWORD_APP:
        print("❌ Faltan ZARA_EMAIL / ZARA_EMAIL_PASSWORD: no se puede enviar el correo")
        return False
    msg = MIMEText(f"¡HAY STOCK EN TALLA {talla}!\n\nProducto: {producto_nombre}\n\nCompra aquí:\n{producto_url}\n\nFecha: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    msg['Subject'] = f"🎉 ¡ALERTA ZARA! {producto_nombre} - Talla {talla} DISPONIBLE"
    msg['From'] = TU_EMAIL
    msg['To'] = EMAIL_DESTINO

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(TU_EMAIL, TU_PASSWORD_APP)
        server.send_message(msg)
        server.quit()
        print("✅ ¡Correo enviado!")
        return True
    except Exception as e:
        print(f"❌ Error enviando mail: {e}")
        return False

def cargar_estado_previo():
    """Carga el estado previo desde archivo JSON"""
    if os.path.exists(ARCHIVO_ESTADO):
        try:
            with open(ARCHIVO_ESTADO, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def guardar_estado(estado):
    """Guarda el estado actual en archivo JSON"""
    with open(ARCHIVO_ESTADO, 'w', encoding='utf-8') as f:
        json.dump(estado, f, indent=2, ensure_ascii=False)

# Estados de talla que Zara considera realmente comprables.
# Cualquier otro valor (out_of_stock, coming_soon, back_soon, ...) NO es stock:
# "coming_soon" es la talla que sólo deja apuntarte para recibir un aviso.
ESTADOS_DISPONIBLES = {
    'in_stock',
    'instock',
    'in stock',
    'low_on_stock',
    'lowonstock',
    'low on stock',
    'schema.org/instock',
    'http://schema.org/instock',
    'https://schema.org/instock',
}


# Tallas cuyo estado no se ha podido leer en la última revisión.
# Si Zara bloquea al bot o cambia el formato de sus datos, el resultado sería
# "no disponible" para todo y el run acabaría en verde sin que te enteres:
# esta lista permite distinguir "agotado" de "no he podido mirarlo".
TALLAS_NO_LEIDAS = []


def _talla_disponible(availability):
    """Traduce el campo availability de Zara a disponible/no disponible."""
    valor = str(availability).strip().lower()
    if valor in ESTADOS_DISPONIBLES:
        return True
    if valor.split('/')[-1] in ESTADOS_DISPONIBLES:
        return True
    return False


def _extraer_arrays(html, clave):
    """Extrae los arrays JSON asociados a una clave ("sizes", "productMetaData"...).

    Recorre el HTML equilibrando corchetes en vez de usar un regex perezoso, que
    se rompe en cuanto Zara cambia el orden de los campos.
    """
    arrays = []
    marca = '"%s":[' % clave
    pos = html.find(marca)
    while pos != -1:
        inicio = pos + len(marca) - 1
        nivel = 0
        dentro_string = False
        escapado = False
        for i in range(inicio, len(html)):
            c = html[i]
            if escapado:
                escapado = False
                continue
            if c == '\\':
                escapado = True
                continue
            if c == '"':
                dentro_string = not dentro_string
                continue
            if dentro_string:
                continue
            if c == '[':
                nivel += 1
            elif c == ']':
                nivel -= 1
                if nivel == 0:
                    arrays.append(html[inicio:i + 1])
                    break
        pos = html.find(marca, pos + 1)
    return arrays


def verificar_disponibilidad_talla(driver, talla):
    """Verifica si una talla concreta está realmente disponible para comprar."""
    try:
        html = driver.page_source
        talla_buscada = talla.strip().upper()

        print(f"   🔎 Buscando datos JSON del producto para talla {talla}...")

        import re

        # MÉTODO 1: JSON-LD Schema.org
        json_pattern = r'<script type="application/ld\+json">(.*?)</script>'
        for json_str in re.findall(json_pattern, html, re.DOTALL):
            try:
                data = json.loads(json_str)
            except Exception:
                continue

            products = data if isinstance(data, list) else [data]
            for product in products:
                if not isinstance(product, dict) or product.get('@type') != 'Product':
                    continue

                tallas_producto = product.get('size', '')
                if isinstance(tallas_producto, list):
                    nombres = [str(t).strip().upper() for t in tallas_producto]
                else:
                    nombres = [str(tallas_producto).strip().upper()]

                # Si el bloque agrupa varias tallas, su availability es del
                # producto entero, no de la talla: no sirve para decidir.
                if len(nombres) != 1 or nombres[0] != talla_buscada:
                    continue

                offers = product.get('offers', {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                availability = offers.get('availability', '')

                # Sin dato de disponibilidad seguimos con el JSON interno de Zara.
                if not str(availability).strip():
                    continue

                print(f"      📊 Talla {talla} encontrada en JSON-LD")
                print(f"         Disponibilidad: {availability}")
                if _talla_disponible(availability):
                    print(f"      ✅ Talla {talla}: DISPONIBLE")
                    return True
                print(f"      ❌ Talla {talla}: NO comprable ({availability})")
                return False

        # MÉTODOS 2 y 3: JSON interno de Zara (productMetaData / sizes)
        for clave, campo_nombre in (('productMetaData', 'sizeName'), ('sizes', 'name')):
            for array_str in _extraer_arrays(html, clave):
                try:
                    items = json.loads(array_str)
                except Exception:
                    continue

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    nombre_talla = str(item.get(campo_nombre, '')).strip().upper()
                    if nombre_talla != talla_buscada:
                        continue

                    availability = item.get('availability', '')
                    print(f"      📊 Talla {talla} encontrada en {clave}")
                    print(f"         Disponibilidad: {availability}")

                    if _talla_disponible(availability):
                        print(f"      ✅ Talla {talla}: DISPONIBLE")
                        return True

                    if str(availability).strip().lower() in ('coming_soon', 'coming soon', 'back_soon'):
                        print(f"      ⏳ Talla {talla}: sólo 'próximamente' / avisadme (NO es stock)")
                    else:
                        print(f"      ❌ Talla {talla}: AGOTADA ({availability})")
                    return False

        print(f"   ⚠️ Talla {talla} NO encontrada en los datos JSON de la página")
        TALLAS_NO_LEIDAS.append(talla)
        return False

    except Exception as e:
        print(f"⚠️ Error verificando talla {talla}: {e}")
        TALLAS_NO_LEIDAS.append(talla)
        return False


def buscar_stock_producto(driver, producto, estado_previo):
    """Busca stock de un producto específico"""
    nombre = producto['nombre']
    url = producto['url']
    tallas = producto['tallas']
    
    # Clave única para este producto en el estado
    producto_key = url.split('/')[-1].split('?')[0]  # Extrae ID del producto de la URL
    
    print(f"\n{'─'*60}")
    print(f"📦 Producto: {nombre}")
    print(f"🔗 URL: {url}")
    print(f"👕 Tallas a monitorear: {', '.join(tallas)}")
    print(f"{'─'*60}\n")
    
    estado_producto_previo = estado_previo.get(producto_key, {})
    estado_producto_actual = {}
    cambios_detectados = []
    
    try:
        driver.get(url)
        print("⏳ Cargando página...")
        time.sleep(12)
        
        # Scroll para activar carga lazy
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(2)
        
        # Manejar popup de cookies si aparece
        try:
            boton_cookies = driver.find_element(By.XPATH, "//button[contains(text(), 'Aceptar') or contains(text(), 'Accept')]")
            boton_cookies.click()
            time.sleep(2)
        except:
            pass
        
        # Verificar cada talla
        for talla in tallas:
            print(f"🔍 Verificando talla {talla}...")
            disponible = verificar_disponibilidad_talla(driver, talla)
            estado_producto_actual[talla] = disponible
            
            # Comparar con estado previo
            estaba_disponible = estado_producto_previo.get(talla, False)
            
            if disponible:
                print(f"   ✅ Talla {talla}: DISPONIBLE")
            else:
                print(f"   ❌ Talla {talla}: AGOTADA")
            
            # Detectar cambio de agotado → disponible
            if not estaba_disponible and disponible:
                print(f"   🎉 ¡CAMBIO DETECTADO! Talla {talla} ahora está DISPONIBLE")
                cambios_detectados.append((talla, nombre, url))
        
        print()
        return estado_producto_actual, cambios_detectados
        
    except Exception as e:
        print(f"⚠️ Error procesando producto: {e}")
        return estado_producto_actual, cambios_detectados

def buscar_stock():
    """Busca stock de todos los productos configurados"""
    print(f"\n{'='*60}")
    print(f"🔎 Iniciando búsqueda: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'='*60}\n")
    
    # Cargar productos
    productos = cargar_productos()
    TALLAS_NO_LEIDAS.clear()
    
    if not productos:
        print("❌ No hay productos para monitorear")
        return
    
    print(f"📋 Productos a monitorear: {len(productos)}")
    
    driver = crear_driver()
    estado_previo = cargar_estado_previo()
    estado_actual = {}
    todos_cambios = []
    
    try:
        # Procesar cada producto
        for idx, producto in enumerate(productos, 1):
            print(f"\n🔄 [{idx}/{len(productos)}]")
            
            producto_key = producto['url'].split('/')[-1].split('?')[0]
            estado_producto, cambios = buscar_stock_producto(driver, producto, estado_previo)
            
            # Guardar estado de este producto
            estado_actual[producto_key] = estado_producto
            
            # Acumular cambios
            todos_cambios.extend(cambios)
            
            # Pausa entre productos para no sobrecargar
            if idx < len(productos):
                print("⏸️  Esperando 3 segundos antes del siguiente producto...")
                time.sleep(3)
        
        # Enviar notificaciones de todos los cambios
        if todos_cambios:
            print(f"\n{'='*60}")
            print(f"🎉 ¡Se detectaron {len(todos_cambios)} cambios!")
            print(f"{'='*60}\n")
            
            for talla, nombre, url in todos_cambios:
                notificacion_sistema(
                    "¡STOCK DISPONIBLE EN ZARA!",
                    f"{nombre} - Talla {talla} disponible"
                )
                enviar_correo(talla, nombre, url)
        else:
            print(f"\n{'='*60}")
            print("📊 Sin cambios detectados en ningún producto")
            print(f"{'='*60}\n")
        
        # Guardar estado actual
        guardar_estado(estado_actual)
        
        return estado_actual

    except Exception as e:
        print(f"⚠️ Error en el proceso: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        try:
            print("🔒 Cerrando navegador...")
            driver.quit()
            time.sleep(2)  # Esperar a que se cierre completamente
        except Exception as e:
            print(f"⚠️ Error cerrando driver: {e}")
            # Intentar matar procesos de ChromeDriver zombies
            try:
                import subprocess
                subprocess.run(['pkill', '-9', 'chromedriver'], capture_output=True)
            except:
                pass

def monitoreo_continuo():
    """Ejecuta el monitoreo en bucle"""
    productos = cargar_productos()
    
    print("🤖 Iniciando monitoreo continuo de Zara Bot")
    print(f"📦 Productos configurados: {len(productos)}")
    
    for idx, producto in enumerate(productos, 1):
        print(f"   [{idx}] {producto['nombre']} - Tallas: {', '.join(producto['tallas'])}")
    
    print(f"⏱️  Intervalo: cada {INTERVALO_MINUTOS} minutos")
    print(f"\n{'='*60}\n")
    
    iteracion = 0
    while True:
        iteracion += 1
        print(f"📍 Iteración #{iteracion}")
        
        try:
            buscar_stock()
        except KeyboardInterrupt:
            print("\n\n⚠️ Deteniendo monitoreo...")
            break
        except Exception as e:
            print(f"❌ Error en iteración: {e}")
            import traceback
            traceback.print_exc()
            
            # Limpiar procesos ChromeDriver zombies
            print("🧹 Limpiando procesos ChromeDriver...")
            try:
                import subprocess
                subprocess.run(['pkill', '-9', 'chromedriver'], capture_output=True)
                time.sleep(2)
            except:
                pass
        
        print(f"\n⏸️  Esperando {INTERVALO_MINUTOS} minutos hasta la próxima revisión...")
        print(f"   (Presiona Ctrl+C para detener)\n")
        
        try:
            time.sleep(INTERVALO_MINUTOS * 60)
        except KeyboardInterrupt:
            print("\n\n⚠️ Deteniendo monitoreo...")
            break

# Ejecución
if __name__ == "__main__":
    # --once (o ZARA_MODO=once) hace una sola revisión sin preguntar nada:
    # es lo que usa GitHub Actions, que no puede responder a un input().
    modo = os.environ.get("ZARA_MODO", "").strip().lower()
    if "--once" in sys.argv:
        modo = "once"
    elif "--continuo" in sys.argv:
        modo = "continuo"

    if not modo and not sys.stdin.isatty():
        modo = "once"  # sin terminal interactiva, una sola pasada

    if not modo:
        print("\n¿Qué deseas hacer?")
        print("1. Ejecutar UNA revisión de stock")
        print("2. Monitoreo CONTINUO (cada X minutos)")
        modo = "continuo" if input("\nElige (1 o 2): ").strip() == "2" else "once"

    if modo == "continuo":
        monitoreo_continuo()
    else:
        buscar_stock()
        if TALLAS_NO_LEIDAS:
            print("\n" + "=" * 60)
            print(f"⚠️ No se pudo leer el estado de: {', '.join(TALLAS_NO_LEIDAS)}")
            print("   Zara puede haber bloqueado la petición o cambiado su formato.")
            print("   El bot NO puede garantizar que te avise: revisa el log.")
            print("=" * 60)
            sys.exit(1)  # marca el run en rojo para que GitHub te avise por correo
        print("\n✅ Revisión completada.")
