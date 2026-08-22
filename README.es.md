# PS4 PKG Installer

Enviá archivos `.pkg` desde tu computadora a una PS4 con jailbreak por la red local, con progreso real, pausa y cancelación.

Multiplataforma (macOS, Windows, Linux), un solo archivo Python, una sola dependencia.

**[Read this in English](README.md)**

---

## Por qué otro sender

Ya hay herramientas buenas — [DirectPackageInstaller](https://github.com/marcussacana/DirectPackageInstaller) y PS4 PKG Sender, entre otras. Esta existe porque el consejo habitual de "levantá un `python3 -m http.server` y listo" **rompe las instalaciones en silencio**, y el error que ves apunta para cualquier lado menos al verdadero.

El `SimpleHTTPRequestHandler` de Python no soporta peticiones HTTP `Range`. Responde `200 OK` con el archivo entero sin importar qué rango de bytes le hayas pedido.

La consola lee los metadatos del paquete pidiendo offsets específicos dentro del archivo. De [`pkg.c`](https://github.com/Backporter/ps4_remote_pkg_installer-OOSDK) del Remote PKG Installer:

```c
http_download_file(piece_urls[0], &param_sfo_data, &param_sfo_dl_size, NULL, param_sfo_offset);
```

El `param.sfo` vive en un offset arbitrario, a varios megabytes del arranque. Si le pedís esos bytes a un servidor Python pelado, te devuelve los del offset cero. La consola no puede parsear lo que recibe y reporta:

```
Unable to load system file object for package '...'
```

Que suena a problema de firmware o a archivo corrupto. No es ninguna de las dos cosas. Y peor: `http.c` acepta el código de estado equivocado sin chistar:

```c
return (status_code == 200 || status_code == 206);
```

Así que nada te avisa. Esta herramienta trae un servidor HTTP que implementa `Range` como corresponde (`206 Partial Content`, `Content-Range`, `416` cuando el rango no existe), y eso es lo que hace que funcione.

---

## Requisitos

**En la PS4**

- Consola con jailbreak y GoldHEN.
- El homebrew **Remote Package Installer** instalado — el `.pkg` lo bajás de **[pkg-zone (title ID `FLTZ00003`)](https://pkg-zone.com/details/FLTZ00003)**. Ojo que el [repo de flatz](https://github.com/flatz/ps4_remote_pkg_installer) no publica releases, solo el código fuente, así que el paquete compilado sale de las tiendas de homebrew. En firmware reciente, mejor una build hecha con el [OOSDK](https://github.com/Backporter/ps4_remote_pkg_installer-OOSDK).

  El huevo y la gallina: esta app no la podés instalar con ella misma. Usá un pendrive, o el *Debug Settings → Package Installer → Install From HTTP* de GoldHEN apuntando a cualquier servidor web local.
- Esa app **abierta y en primer plano**. No es opcional. Su propia documentación es explícita:

  > To be able to use this tool for receiving commands you need to have this application in focus (not in a background, because PS4 will suspend it and it won't be possible to use network anymore).

  Apretás el botón PS y el puerto se muere.

**En tu computadora**

- Python 3.8 o superior
- `flet` (se instala abajo)
- Misma red que la consola. Para paquetes de varios gigas, el cable le saca años de ventaja al Wi‑Fi.

---

## Instalación

### Binario ya compilado — sin necesidad de Python

Bajate el archivo de tu plataforma desde la **[última release](https://github.com/KonixDev/ps4-pkg-installer/releases/latest)**:

| Plataforma | Archivo |
|---|---|
| macOS (Apple Silicon) | `ps4-pkg-installer-macos-arm64.zip` |
| macOS (Intel) | `ps4-pkg-installer-macos-x64.zip` |
| Windows | `ps4-pkg-installer-windows-x64.zip` |
| Linux | `ps4-pkg-installer-linux-x64.tar.gz` |

Las builds de macOS van sin firmar, así que Gatekeeper va a protestar la primera vez. Clic derecho → *Abrir*, o:

```bash
xattr -dr com.apple.quarantine "PS4 PKG Installer.app"
```

### Desde el código fuente

```bash
git clone https://github.com/KonixDev/ps4-pkg-installer.git
cd ps4-pkg-installer
pip install -r requirements.txt
python3 ps4_pkg_installer.py
```

Eso es todo. Sin Node, sin .NET, sin compilar nada.

### Compilarlo vos mismo

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name "PS4 PKG Installer" \
  --collect-all flet --collect-all flet_desktop ps4_pkg_installer.py
```

---

## Cómo se usa

**1. Apuntala a tus paquetes.** Tocá *Cambiar* y elegí la carpeta. Busca también en las subcarpetas, así que podés apuntarla a un directorio padre con varios juegos adentro. Los archivos con extensión `.pkg` que no tengan la firma de paquete de PS4 (`7F 43 4E 54`) se descartan y se nombran en la consola, para que no se te cuelen instaladores sueltos ni artefactos de compilación.

**2. Encontrá la consola.** Escribí su IP, o tocá el ícono de radar para barrer la subred buscando algo que escuche en el puerto 12800.

**3. Mirá los chips de estado.** Son dos, arriba a la derecha:

- `Servidor :8000` — el servidor local de archivos. Arranca solo, no hay botón. Si el puerto está ocupado prueba los siguientes y te dice con cuál se quedó.
- `PS4 lista` — la consola. Ver abajo.

**4. Tildá los paquetes que quieras y tocá Instalar.**

Cada fila pasa a mostrar su propio estado, barra de progreso, porcentaje, bytes transferidos y tiempo restante, con botones de pausa y cancelación. La consola baja varios paquetes a la vez, así que vas a ver varias filas avanzando juntas.

---

## Entender el estado de la consola

Tres estados, y la diferencia importa:

| Chip | Qué significa |
|---|---|
| `PS4 lista` | La app respondió a una llamada real de la API. Todo en orden. |
| `PS4 trabada` | El puerto está abierto pero la app no contesta. |
| `PS4 no responde` | No hay nada en el puerto 12800. |

**`PS4 trabada` es el que conviene conocer.** El Remote PKG Installer corre sobre `sandbird`, un event loop de un solo hilo, y hace toda la descarga de metadatos *adentro* del handler del pedido. Un pedido que se cuelga deja el servidor muerto para siempre — mientras el puerto TCP sigue abierto, porque el handshake lo completa el kernel, esté la app viva o no.

Un chequeo por `connect()` TCP te dice "conectado" contra una app completamente muerta. Esta herramienta manda un `POST /api/is_exists` de verdad, así que distingue los casos y te avisa que reinicies la app en la consola.

---

## Progreso y control

`/api/install` devuelve un `task_id`. Con eso, la herramienta consulta `/api/get_task_progress` para obtener la visión que tiene la propia consola de la transferencia — bytes movidos, segundos restantes, porcentaje de descompresión — y por eso las fases se leen como *Preparando → Descargando → Instalando* en vez de una sola barra indistinta. *Instalando* es el `local_copy_percent`: la consola descomprimiendo una vez que los bytes ya llegaron.

Ese mismo `task_id` alimenta `/api/pause_task`, `/api/resume_task` y `/api/stop_task`, de donde salen los botones de cada fila y el *Cancelar todo*.

**Una advertencia:** las respuestas de RPI no son JSON válido. Escribe los enteros como literales hexadecimales:

```c
sb_writef(s, "{ \"status\": \"fail\", \"error_code\": 0x%08X }\n", code);
```

`0x80990085` no es JSON, así que `json.loads()` muere en la `x` con `Expecting ',' delimiter: line 1 column 36 (char 35)` — y un error legítimo de la consola aparece como un error de parseo de Python. (DirectPackageInstaller lo esquiva buscando el string `"success"` en vez de parsear.) Esta herramienta convierte los hexa antes de parsear, así que los códigos reales llegan y se traducen:

| Código | Significado |
|---|---|
| `0x80990085` | No hay espacio suficiente en la PS4 |
| `0x80990088` | Ya existe una tarea para ese contenido |
| `0x8099000E` | El contenido ya está instalado |

---

## Problemas frecuentes

**"Unable to load system file object"** — la consola bajó el `param.sfo` del paquete y no pudo parsearlo. Casi siempre es un servidor que no respeta `Range`, que es justamente lo que esta herramienta arregla. Si te sigue pasando usándola, lo más probable es que el `.pkg` esté truncado o corrupto.

**La instalación da timeout y en la consola no aparece ningún pedido de descarga** — la app de la PS4 está trabada. Cerrala y volvé a abrirla. Tocá *Probar* primero; te lo va a decir.

**Firmware incompatible** — un paquete compilado para un firmware más nuevo que el de tu consola va a ser rechazado. Los paquetes viejos corren en firmware nuevo; al revés no. Ojo: ese fallo **no** es el error de "system file object" de más arriba — ese ocurre antes, mientras lee los metadatos.

**La transferencia se corta a mitad de camino** — no dejes que la computadora se suspenda. En macOS, `caffeinate -i` mientras dure.

---

## Notas

Dejá la app abierta durante toda la transferencia. Tu máquina es el servidor de archivos: si la cerrás, la descarga se muere. El gestor de descargas de la consola trabaja en segundo plano, así que del lado de la PS4 sigue solo una vez registrada la tarea.

Editar el código mientras hay una transferencia en curso es seguro — Python lee el archivo una sola vez al arrancar y no lo mantiene abierto. Los cambios aplican en la siguiente ejecución.

---

## Créditos

- [flatz](https://github.com/flatz/ps4_remote_pkg_installer) — el Remote Package Installer original
- [Backporter](https://github.com/Backporter/ps4_remote_pkg_installer-OOSDK) — build OOSDK para firmware reciente
- [marcussacana](https://github.com/marcussacana/DirectPackageInstaller) — DirectPackageInstaller, la referencia de cómo manejar `Range` bien del lado del servidor

## Aclaración

Es una utilidad de transferencia de archivos para consolas con homebrew habilitado. Usala con contenido que tengas derecho a usar. No elude ninguna protección ni distribuye material con copyright.

## Licencia

MIT — ver [LICENSE](LICENSE).
