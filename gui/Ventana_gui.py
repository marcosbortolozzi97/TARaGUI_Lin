
# gui/Ventana_gui.py
"""
Ventana principal de la aplicación TAR GUI.

Esta es la clase que conecta TODO. Sus responsabilidades:
    - Construir la GUI (paneles izquierdo y derecho).
    - Crear y manejar la fuente de datos (SerialSource o ReplayBinSource).
    - Crear y controlar la sesión del ensayo (EnsayoSession).
    - Ejecutar el loop periódico (_tick_ensayo) que mantiene todo al día.
    - Orquestrar los bloqueos / desbloqueos de la GUI según el estado del ensayo.

Patrón de comunicación:
    GUI (paneles) --callbacks--> MainWindow --métodos--> EnsayoSession / Fuentes
    EnsayoSession --datos-->    MainWindow --actualiza--> GUI (paneles)

Los paneles no hablan entre sí ni con el hardware. MainWindow es el
único que sabe el estado global y decide qué hacer.
"""

from email import header
from sys import platform
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import time
from pathlib import Path
from PIL import Image, ImageTk, ImageFilter, ImageEnhance

from gui.Panel_Serial      import SerialPanel
from gui.Panel_Ensayo      import PanelEnsayo
from gui.Panel_Parametros  import PanelParametros
from gui.Panel_Histograma  import PanelHistograma

from core.ensayo_sesion          import EnsayoSession, TARMode
from core.Fuentes.fuente_serie   import SerialSource
from core.Fuentes.replay_bin     import ReplayBinSource


class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("TAR GUI")
        
        # Configuración de ventana según plataforma
        import platform
        if platform.system() == "Linux":
            screen_width = self.winfo_screenwidth()
            screen_height = self.winfo_screenheight()
            
            # Ajustar según resolución de pantalla
            if screen_height <= 768:
                # Pantalla pequeña (1366x768, 1024x768)
                height_percent = 0.80
            elif screen_height <= 1080:
                # Pantalla Full HD (1920x1080)
                height_percent = 0.85
            else:
                # Pantalla grande (2K, 4K)
                height_percent = 0.88
            
            window_width = int(screen_width * 0.95)
            window_height = int(screen_height * height_percent)
            
            x = (screen_width - window_width) // 2
            y = (screen_height - window_height) // 2
            
            self.geometry(f"{window_width}x{window_height}+{x}+{y}")
        else:
            try:
                self.state('zoomed')
            except tk.TclError:
                pass

        # ---------------------------------
        # Estado interno de MainWindow
        # ---------------------------------
        self.fuente        = None   # Fuente activa: apunta a serial_source o a un ReplayBinSource
        self.serial_source = None   # Fuente serie persistente (se mantiene entre ensayos LIVE)
        self.ensayo: EnsayoSession | None = None   # Sesión activa o None

        # Control temporal del ensayo LIVE:
        self._temp              = None    # Timestamp (time.time) cuando debe autostopear (None = sin límite)
        self._ensayo_activo     = False   # True mientras hay ensayo en curso o cerrándose
        self._stop_solicitado   = False   # Evita enviar STOP doble (timeout y botón simultáneos)
        self._t_inicio_live     = None    # time.time() del momento en que inició el ensayo
        self.duracion_total     = 0       # Duración pedida por el usuario (para el feedback visual)

        # Parámetros de histéresis guardados por _aplicar_parametros.
        # Se envían al TAR cuando el ensayo inicia, no cuando el usuario presiona Aplicar.
        self._params_histeresis = {}

        # Cierre limpio: si el usuario cierra la ventana con un ensayo activo,
        # se detiene antes de destruir la ventana.
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # ---------------------------------
        # Construcción de la GUI
        # ---------------------------------
        self._construir_ui()

        # Estado inicial de los botones que deben estar deshabilitados al arrancar
        self.ensayo_panel.boton_finalizar.config(state="disabled")
        self.ensayo_panel.boton_get_conf.config(state="disabled")

        # Inicia el loop periódico. after() no bloquea: programa la llamada
        # y la GUI sigue respondiendo al usuario mientras tanto.
        self.after(200, self._tick_ensayo)


    # ==================================================
    # Construcción de la GUI
    # ==================================================
    def _construir_ui(self):
        
        # ══════════════════════════════════════════════════════════════
        # Header con logos institucionales
        # ══════════════════════════════════════════════════════════════
        header = tk.Frame(self, bg="#000000")  # Negro 
        header.pack(fill="x", pady=(0, 0))
        
        # Configuración del grid: 3 columnas (izq, centro, der)
        header.columnconfigure(0, weight=1)  # Izquierda: crece
        header.columnconfigure(1, weight=0)  # Centro: fijo
        
        # ── Función helper para cargar logos ──
        def cargar_logo(filename, size):
            try:
                path = Path("logos") / filename
                if not path.exists():
                    return None
        
                img = Image.open(path).convert("RGBA") # Asegura compatibilidad de color
        
                # 1. Redimensionar con Lanczos (Ya lo tenés, es el mejor método)
                img.thumbnail(size, Image.Resampling.LANCZOS)
        
                # 2. Aplicar filtro de nitidez por convolución
                img = img.filter(ImageFilter.SHARPEN)
        
                # 3. (Opcional) Aumentar el contraste y la claridad específicamente
                enhancer = ImageEnhance.Sharpness(img)
                img = enhancer.enhance(2.0)  # Factor de 2.0 para mayor nitidez
        
                return ImageTk.PhotoImage(img)
            except Exception as e:
                return None
        
        # ── Logos panel izquierdo ──────────
        frame_izq = tk.Frame(header, bg="#000000")
        frame_izq.grid(row=0, column=0, sticky="w", padx=15)
        
        self.logos_instituciones = []  # Mantener referencias para evitar garbage collection
        logos_izq = [
            ("logo_UNR.png", (40, 40)),
            ("FCEIA_logo.png", (60, 40)),
            ("DSI_logo.png", (41, 41)),
            ("logo_IENRI.png", (65, 55)),
            ("RA4_logo.png", (70, 40)),
            ("CNEA_logo.png", (42, 37.5))
        ]
        
        for idx, (filename, size) in enumerate(logos_izq):
            logo = cargar_logo(filename, size)
            if logo:
                self.logos_instituciones.append(logo)
                lbl = tk.Label(frame_izq, image=logo, bg="#000000", bd=0)
                lbl.pack(side="left", padx=8)

        
        # ── Botones de Información y Ayuda ──────────────
        frame_der = tk.Frame(header, bg="#000000")
        frame_der.grid(row=0, column=2, sticky="e", padx=15)
        
        # Botón Ayuda 
        self.btn_ayuda = tk.Button(
            frame_der, text="?", font=("Arial", 14, "bold"),
            bg="#FFFFFF", fg="black", activebackground="#E78B00",
            activeforeground="black", bd=0, width=2, cursor="hand2",
            relief="flat", command=self._mostrar_ayuda
        )
        self.btn_ayuda.pack(side="left", padx=5)

        # NUEVO: Botón Acerca de
        self.btn_acerca_de = tk.Button(
            frame_der, text="ⓘ", font=("Arial", 14, "bold"),
            bg="#FFFFFF", fg="black", activebackground="#E78B00",
            activeforeground="black", bd=0, width=2, cursor="hand2",
            relief="flat", command=self._mostrar_acerca_de
        )
        self.btn_acerca_de.pack(side="left", padx=5)

        header.update_idletasks()
        altura_real = header.winfo_reqheight()
        header.config(height=altura_real)
        header.pack_propagate(False)
        
        # ---------------------------------
        # Contenedor principal 
        # ---------------------------------
        # Layout en dos columnas: izquierda fija (controles), derecha elástica (gráficos).
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)

        # ── Columna izquierda: paneles de control ────────────────────
        left_panel = ttk.Frame(container)
        left_panel.grid(row=0, column=0, sticky="n")   # sticky="n" = se alinea arriba

        ttk.Label(
            left_panel,
            text="PANEL DE CONTROL",
            font=("TkDefaultFont", 13, "bold")
        ).pack(pady=0, padx=100, anchor="center")

        # left_inner es el contenedor real donde se empaquetan los paneles funcionales.
        # La separación left_panel / left_inner permite tener el título fuera de los paneles.
        left_inner = ttk.Frame(left_panel)
        left_inner.pack(expand=True)

        # Panel serie: on_connect y on_disconnect son callbacks que MainWindow define abajo.
        # SerialPanel no sabe ni le importa qué hace MainWindow: solo notifica.
        self.serial_panel = SerialPanel(
            left_inner,
            on_connect_callback=self._seleccionar_serial,
            on_disconnect_callback=self._desconectar_serial
        )
        self.serial_panel.pack(pady=(0,3))

        # Panel parámetros: on_apply_params guarda los valores en MainWindow
        self.param_panel = PanelParametros(
            left_inner,
            on_apply_params_callback=self._aplicar_parametros
        )
        self.param_panel.pack(pady=3, fill="x")

        # Panel ensayo: recibe todos los callbacks de control del ensayo.
        # validar_inicio_callback es especial: retorna (bool, str) y se llama
        # desde PanelEnsayo ANTES de on_iniciar para verificar precondiciones.
        self.ensayo_panel = PanelEnsayo(
            left_inner,
            on_iniciar_callback=self.iniciar_ensayo,
            on_finalizar_callback=self.finalizar_ensayo,
            on_cargar_crudo_callback=self.cargar_bin_replay,
            on_get_conf_callback=self._mostrar_configuracion,
            validar_inicio_callback=self._validar_inicio
        )
        self.ensayo_panel.pack(pady=3)

        # ── Columna derecha: gráficos ────────────────────────────────
        right_panel = ttk.Frame(container)
        right_panel.grid(row=0, column=1, sticky="nsew")   # nsew = crece en todas las direcciones

        ttk.Label(
            right_panel,
            text="GRÁFICAS",
            font=("TkDefaultFont", 13, "bold")
        ).pack(pady=0, padx=8, anchor="center")

        # El panel histograma empieza sin ensayo conectado (None).
        # Se conecta cuando inicia un ensayo via set_ensayo().
        self.panel_histograma = PanelHistograma(
            right_panel,
            ensayo_session=None
        )
        self.panel_histograma.pack(fill="both", expand=True, padx=10)


    # ==================================================
    # Popup de configuración TAR
    # ==================================================
    def _popup_configuracion(self, conf: dict):
        """
        Ventana flotante no modal que muestra la histéresis actual.
        transient(self) la hace dependiente de la ventana principal:
        si se cierra MainWindow, este popup se cierra automáticamente.
        """
        win = tk.Toplevel(self)
        win.title("Configuración actual del TAR")
        win.resizable(False, False)
        win.transient(self)

        main = ttk.Frame(win, padding=15)
        main.pack(fill="both", expand=True)

        ttk.Label(
            main,
            text="AXI_TAR",
            font=("TkDefaultFont", 11, "bold")
        ).pack(anchor="w", pady=(0, 8))

        txt = (
            f"CHA: histéresis ({conf['CHA']['min']} ; {conf['CHA']['max']}) cuentas\n"
            f"CHB: histéresis ({conf['CHB']['min']} ; {conf['CHB']['max']}) cuentas"
        )
        ttk.Label(main, text=txt, justify="left", font=("TkDefaultFont", 10)).pack(anchor="w")


    # ==================================================
    # Mostrar ayuda / manual de usuario
    # ==================================================
    def _mostrar_ayuda(self):
        """
        Muestra ventana de ayuda/manual de usuario.
        Contiene instrucciones básicas de operación del TAR.
        """
        # Crear ventana modal
        win = tk.Toplevel(self)
        win.title("Manual de Usuario - TAR GUI")
        win.geometry("700x600")
        win.resizable(True, True)
        win.transient(self)
        win.grab_set()  # Modal: bloquea interacción con ventana principal
        
        # Frame principal con scrollbar
        main_frame = ttk.Frame(win)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Canvas + Scrollbar para contenido largo
        canvas = tk.Canvas(main_frame, bg="white")
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # ---------------------------------
        # CONTENIDO DEL MANUAL
        # ---------------------------------
        
        # Título
        ttk.Label(
            scrollable_frame,
            text="Manual de Operación - Interfaz para TAR",
            font=("TkDefaultFont", 14, "bold"),
            foreground="#1f77b4"
        ).pack(pady=(0, 10))
        
        # Sección 0: Configuración inicial
        self._agregar_seccion(scrollable_frame, 
            "CONFIGURACIÓN INICIAL",
            """
Condiciones previas antes de iniciar un ensayo LIVE:
• Debe estar conectado el TAR a puerto USB de la PC.
• Debe seleccionar y concectar el Puerto en Panel Serial. El estado se actualiza a Puerto Conectado.
• Parámetros de configuración del TAR almacenados correctamente (debe generarse mensaje temporal 
de confirmación) en Panel Parámetros luego de rellenar los campos y aplicar con valores correctos. 
• En Panel Ensayo debe configurar la duración del ensayo en segundos en caso de tiempo requerido 
o dejar vacío para duración infinita.
• El ensayo se inicia desde el Panel Ensayo, presionando "Iniciar ensayo". Si aparece mensaje de 
advertencia debe revisar si el puerto seleccionado es el correcto y los campos mencionados anteriormente.
• Puede configurar la visualización de los histogramas en tiempo real desde el Panel Histograma 
antes o despues de un ensayo.
• El ensayo se detiene automáticamente al cumplirse la duración requerida o manualmente presionando 
"Finalizar ensayo".
• La condición única para procesar un ensayo previo (REPLAY) es que el mismo haya sido guardado 
mediante un ensayo en vivo. Se realiza abriendo el archivo *.bin.
            """
        )

        # Sección 1: Conexión
        self._agregar_seccion(scrollable_frame, 
            "1. CONEXIÓN AL DISPOSITIVO TAR",
            """
Luego de realizar la conexión física entre el TAR y la PC, siga estos pasos:
• En el Panel de Control, seleccionar el puerto COM correcto.
• Si desea cambiar de puerto o desconectar, usar el botón "Desconectar" para cerrar la conexión 
actual antes de conectar a otro puerto.
• En caso de no ver el puerto, presionar el botón de refresh (↻) para reescanear los puertos 
disponibles.
• Presionar el botón "Conectar".
• Verificar que aparezca "- Puerto Conectado a COMx -".
• Al presionar desconectar se debe verificar que el estado sea "- Puerto Desconectado -"
            """
        )
        
        # Sección 2: Configuración
        self._agregar_seccion(scrollable_frame,
            "2. CONFIGURACIÓN DE UMBRALES",
            """
Los umbrales definen el rango de pulsos que se registran y guardan:
• Valores en CUENTAS ADC (rango: 0 - 16383, donde 16383 = 52.57V).
• Configurar umbrales Min y Max para cada canal (A y B).
• Presionar "Aplicar parámetros" antes de iniciar ensayo.

            """
        )
        
        # Sección 3: Ensayo LIVE
        self._agregar_seccion(scrollable_frame,
            "3. ENSAYO EN TIEMPO REAL",
            """
Una vez configurado un ensayo:
• Ingresar duración en segundos o dejar vacío para duración infinita.
• Presionar "Iniciar ensayo".
• El sistema enviará comando START al TAR.
• Los histogramas se actualizan en tiempo real.
• Usar "Finalizar ensayo" para detener manualmente el proceso.
• Los datos se guardan automáticamente cada 15 segundos, generando archivos 
CSV y BIN (incrementales).
• Al finalizar un ensayo, se genera un archivo final (CSV y BIN)con toda la 
información acumulada.
            """
        )
        
        # Sección 4: Histogramas
        self._agregar_seccion(scrollable_frame,
            "4. VISUALIZACIÓN DE HISTOGRAMAS",
            """
Para trabajar con los histogramas en tiempo real o en replay, se pueden usar 
las siguientes herramientas de visualización:
• MIN/MAX: ajustan el ZOOM visual.
• Factor keV/mV: para Calibración energética del detector.
• Offset keV: corrección del cero energético.
• Botón "Full": Resetea a rango completo de visualización (0-16383).
• Para confirmar los cambios siempre se debe presionar el botón "Aplicar" del 
correspondiente canal. 
• Contador de pulsos: Muestra cantidad de eventos dentro de los parámetros 
umbrales definidos por usuario en Panel Parámetros durante un ensayo individual.
• Botón "Reset Histogramas": Limpia los datos acumulados en pantalla (no borra 
archivos guardados).
            """
        )
        
        # Sección 5: Replay
        self._agregar_seccion(scrollable_frame,
            "5. PROCESAMIENTO DE DATOS PREVIOS",
            """
Para analizar un ensayo guardado sin necesidad de reconectar al TAR, se puede 
usar el modo REPLAY:
• Presionar "Procesar binario previo" para analizar ensayos guardados.
• Seleccionar archivo .bin del ensayo. Los mismos se encuentran en 
Documents/Ensayos_TAR con la nomenclatura individual Ensayo_DDMMAAAA-HHMMSS.
• El sistema buscará automáticamente test-config.txt asociado. 
• Puede cargar la configuración de umbrales del ensayo en cuestión presionando 
el botón "Consultar configuración TAR".
• Los histogramas se generan procesando todo el archivo. Puede procesar archivos 
bin incrementales o el completo.
            """
        )
        
        # Sección 6: Archivos Guardados
        self._agregar_seccion(scrollable_frame,
            "6. ARCHIVOS GENERADOS",
            """
UBICACIÓN: Documents/Ensayos_TAR/Ensayo_DDMMAAAA-HHMMSS/
• Carpeta con archivos CSV: incrementales cada 15 segundos + archivo final con 
todos los datos correspondiente a ambos canales.
CSV (csv/):
  - test-chA.csv: Datos Canal A (ind, timestamp, amplitud)
  - test-chB.csv: Datos Canal B (ind, timestamp, amplitud)
• Carpeta con archivos BIN: incrementales cada 15 segundos + archivo final con 
todos los datos + configuración de umbrales aplicada.
BIN (bin/):
  - test-raw.bin: Datos crudos binarios
  - test-config.txt: Configuración de umbrales aplicada
            """
        )
        
        # Pie de página
        ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(
            scrollable_frame,
            text="Sistema Digitalizador TAR - Reactor RA-4",
            font=("TkDefaultFont", 9, "italic"),
            foreground="gray"
        ).pack(pady=(0, 5))
        
        # Empaquetar canvas y scrollbar
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # ---------------------------------
        # Habilitar scroll con la rueda del mouse
        # ---------------------------------
        def _on_mousewheel(event):
            """Maneja el evento de scroll del mouse (multiplataforma)."""
            # Linux usa event.num (4=up, 5=down)
            # Windows usa event.delta (positivo=up, negativo=down)
            if event.num == 5 or event.delta < 0:  # Scroll down
                canvas.yview_scroll(1, "units")
            elif event.num == 4 or event.delta > 0:  # Scroll up
                canvas.yview_scroll(-1, "units")
        
        # Bind para Windows y Linux
        canvas.bind_all("<MouseWheel>", _on_mousewheel)  # Windows/Mac
        canvas.bind_all("<Button-4>", _on_mousewheel)     # Linux scroll up
        canvas.bind_all("<Button-5>", _on_mousewheel)     # Linux scroll down
        
        # Limpiar todos los bindings cuando se cierra la ventana
        def _on_closing():
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")
            win.destroy()
        
        win.protocol("WM_DELETE_WINDOW", _on_closing)
        
        # Centrar ventana
        win.update_idletasks()
        x = (win.winfo_screenwidth() // 2) - (win.winfo_width() // 2)
        y = (win.winfo_screenheight() // 2) - (win.winfo_height() // 2)
        win.geometry(f"+{x}+{y}")
    
    
    def _agregar_seccion(self, parent, titulo, contenido):
        """Helper para agregar secciones al manual."""
        # Título de sección
        titulo_frame = tk.Frame(parent, bg="#1B4F72")
        titulo_frame.pack(fill="x", pady=(10, 0))
        
        tk.Label(
            titulo_frame,
            text=titulo,
            font=("TkDefaultFont", 11, "bold"),
            bg="#1B4F72",
            fg="white",
            anchor="w",
            padx=10,
            pady=5
        ).pack(fill="x")
        
        # Contenido
        tk.Label(
            parent,
            text=contenido.strip(),
            font=("TkDefaultFont", "Liberation Sans", "DejaVu Sans", 10),
            justify="left",
            anchor="w",
            bg="white",
            padx=15,
            pady=8
        ).pack(fill="x")


    def _mostrar_acerca_de(self):
        """Popup 'Acerca de' con descripción de instituciones."""

        ventana = tk.Toplevel(self)
        ventana.title("Acerca de TARaGUI")
        ventana.resizable(False, False)
        ventana.configure(bg="#F4F6F7") 
        ventana.grab_set()

        frame = ttk.Frame(ventana, padding=15)
        frame.pack(fill="both", expand=True)

        texto_intro = (
            "TAR - REGISTRADOR DIGITAL DE AMPLITUD Y TIEMPO (Time and Amplitude Recorder)\n"
            "--------------------------------------------------\n\n"
            "EL PROYECTO TAR:\n"
            "El TAR es un dispositivo diseñado para la digitalización de los pulsos de energía\n"
            "provenientes del decaimiento nuclear de muestras radiactivas, reemplazando a los \n"
            "módulos de conteo de pulsos tradicionales. Luego, la información adquirida será \n"
            "enviada a una PC, lo que permitirá una mejor manipulación y aprovechamiento de los datos.\n\n"
            "INTERFAZ (GUI):\n"
            "Esta interfaz permite la visualización en tiempo real y la reproducción de ensayos grabados.\n\n"
        )

        ttk.Label(frame, text=texto_intro, justify="left").pack(anchor="w", pady=(0,10))

        ttk.Label(
            frame,
            text="LOGOS INSTITUCIONALES:",
            font=("TkDefaultFont", 10, "bold")
        ).pack(anchor="w", pady=(5,5))

        # Frame donde irán los logos
        logos_frame = ttk.Frame(frame)
        logos_frame.pack(anchor="w", pady=5)

        instituciones = [
            ("UNR_logo.png",   "Universidad Nacional de Rosario (UNR)"),
            ("FCEIA_logo.png", "Facultad de Ciencias Exactas, Ingeniería y Agrimensura (FCEIA)"),
            ("DSI_logo.png",   "Departamento de Sistemas e Informática (DSI)"),
            ("IENRI_logo.png", "Instituto de Estudios Nucleares y Radiaciones Ionizantes (IERNI)"),
            ("RA4_logo.png",   "Reactor Nuclear RA-4"),
            ("CNEA_logo.png",  "Comisión Nacional de Energía Atómica (CNEA)")
        ]

        self._logos_about = []

        for row, (archivo, descripcion) in enumerate(instituciones):

            img = Image.open(Path("logos") / archivo)
            img.thumbnail((40, 40), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)

            self._logos_about.append(photo)

            lbl_img = tk.Label(logos_frame, image=photo, bd=0)
            lbl_img.grid(row=row, column=0, padx=(0,10), pady=3, sticky="w")

            lbl_txt = ttk.Label(logos_frame, text=descripcion)
            lbl_txt.grid(row=row, column=1, sticky="w", pady=3)

        # Autores
        ttk.Label(
            frame,
            text="\nAUTORES:\n"
                 "• Desarrollo de Hardware y Firmware TAR: Sebastián Gallo\n"
                 "• Desarrollo de Interfaz de Usuario: Marcos Bortolozzi\n\n"
                 "Versión: 1.0.0 | 2026",
            justify="left"
        ).pack(anchor="w", pady=10)


    # ==================================================
    # Fuente serie (conexión física)
    # ==================================================
    def _seleccionar_serial(self, port):
        """
        Callback de SerialPanel cuando el usuario presiona Conectar.
        Instancia y abre la fuente serie. Si falla, le dice al panel que
        se desconectó para que actualice los botones.
        """
        try:
            self.serial_source = SerialSource(port=port, baudrate=115200)
        except Exception as e:
            messagebox.showerror(
                "Error de conexión",
                f"No se pudo abrir el puerto {port}\n\n{e}"
            )
            self.serial_panel.set_desconectado()
            return

        self.serial_source.open()
        self.fuente = self.serial_source   # La fuente activa ahora es la serie
        self.serial_panel.set_conectado(port)


    def _desconectar_serial(self):
        """
        Callback de SerialPanel cuando el usuario presiona Desconectar.
        Si hay un ensayo activo lo detiene primero (no se puede desconectar
        el cable mientras el TAR está emitiendo).
        """
        if self.ensayo and self.ensayo.is_running():
            self.ensayo.stop()

        if self.serial_source:
            self.serial_source.close()
            self.serial_source = None

        self.fuente = None
        self.serial_panel.set_desconectado()


    # ==================================================
    # Modo REPLAY (procesar archivo binario previo)
    # ==================================================
    def cargar_bin_replay(self):
        """
        Callback del botón 'Procesar binario previo'.
            - Procesa TODO el archivo de una vez (sin actualizar GUI)
            - Muestra solo "Procesando..."
            - Actualiza histogramas UNA VEZ al final
            - Resultado: procesamiento casi instantáneo
        """
        filename = filedialog.askopenfilename(
            title="Seleccionar archivo binario TAR",
            filetypes=[("Archivos BIN", "*.bin")],
        )
        if not filename:
            return   # El usuario canceló el explorador

        self._stop_solicitado = False

        # ── Bloqueo de UI durante procesamiento ──────────────────────
        self.param_panel.bloquear(True)
        self.panel_histograma.bloquear(True)
        self.panel_histograma.habilitar_borrar(False)
        self.ensayo_panel.boton_iniciar.config(state="disabled")
        self.ensayo_panel.boton_crudo.config(state="disabled")
        self.ensayo_panel.boton_get_conf.config(state="disabled")
        self.btn_ayuda.config(state="disabled")
    
        # Mostrar mensaje "Procesando..." (sin porcentaje)
        self.ensayo_panel.set_estado(" - Procesando archivo... - ")
        self.update_idletasks()

        # ── Crear fuente y sesión ─────────────────────────────────────
        # CAMBIO CLAVE: chunk_size GRANDE para leer todo de golpe
        # interval_s = 0 para que no haya delays
        self.fuente = ReplayBinSource(
            path=filename,
            chunk_size=1024*1024,  # 1MB por chunk (era 256 bytes)
            interval_s=0           # Sin delay (era 0.01)
        )
        self.ensayo = EnsayoSession(self.fuente, TARMode.REPLAY)

        # ── Buscar configuración asociada ─────────────────────────────
        bin_path  = Path(filename)
        conf_path = bin_path.parent / "test-config.txt"

        if conf_path.exists():
            with open(conf_path, "r", encoding="utf-8") as f:
                conf_text = f.read()
            self.ensayo.load_conf_from_text(conf_text)
        else:
            self.ensayo.clear_conf()

        # ── NUEVO: Procesar TODO el archivo SIN actualizar GUI ────────
        self.panel_histograma.set_ensayo(self.ensayo)
    
        # Flag para indicar que NO queremos updates en tiempo real
        self._replay_en_proceso = True
    
        self.ensayo.start()   # Inicia procesamiento
    
        # procesamos todo el archivo en un loop bloqueante
        while not self.ensayo.has_finished():
            self.ensayo.tick()   # Avanza el procesamiento
    
        # ── Actualizar histograma UNA VEZ con TODOS los datos ────────
        self.ensayo_panel.set_estado(" - Generando histogramas... - ")
        self.update_idletasks()
    
        # Ahora sí, actualizar con todos los datos acumulados
        self.panel_histograma.tick()
    
        # ── Finalizar ─────────────────────────────────────────────────
        self._replay_en_proceso = False
        self._ensayo_activo = False
        self._temp = None
    
        # Desbloquear GUI
        self.ensayo_panel.var_estado.set(" - Binario procesado - ")
        self.ensayo_panel.boton_get_conf.config(state="normal")
        self.ensayo_panel.boton_iniciar.config(state="normal")
        self.ensayo_panel.boton_crudo.config(state="normal")
        self.btn_ayuda.config(state="normal")
        self.param_panel.bloquear(False)
        self.panel_histograma.bloquear(False)
        self.panel_histograma.habilitar_borrar(True)

        self.after(1200, self._reset_estado_ensayo)


    # ==================================================
    # Modo LIVE (ensayo en tiempo real)
    # ==================================================
    def iniciar_ensayo(self, duracion_seg):
        """
        Callback del botón 'Iniciar ensayo' (vía PanelEnsayo._iniciar).
        duracion_seg es None si el campo estaba vacío (ensayo sin fin).

        Flujo:
            1. Crea EnsayoSession con la fuente serie.
            2. Envía los parámetros de histéresis al TAR.
            3. Conecta histogramas.
            4. Inicia el ensayo (envía START al hardware).
            5. Configura el temporizador si hay duración finita.
            6. Bloquea la GUI.
        """
        if not self.serial_source:
            messagebox.showerror("Error", "No hay puerto serie conectado")
            return

        self.fuente = self.serial_source
        self._stop_solicitado = False

        self.ensayo = EnsayoSession(self.fuente, TARMode.LIVE)

        # Enviar histéresis antes de start: el TAR necesita saberla antes de emitir.
        # Si falla (por ejemplo, la fuente no está abierta) se muestra error y se aborta.
        try:
            self.ensayo.apply_hysteresis(self._params_histeresis)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return

        self.panel_histograma.set_ensayo(self.ensayo)
        self.ensayo.start()   # Envía START al TAR, empieza a llegar datos

        # ── Control temporal ─────────────────────────────────────────
        self._t_inicio_live = time.time()

        if duracion_seg is None:
            self._temp          = None    # Sin límite de tiempo: solo el botón Finalizar detiene
            self.duracion_total = None
        else:
            self._temp          = self._t_inicio_live + duracion_seg   # Cuando autodetiene
            self.duracion_total = duracion_seg

        self._ensayo_activo = True

        # ── Bloqueo de UI durante ensayo live ─────────────────────────
        self.param_panel.bloquear(True)
        self.ensayo_panel.boton_iniciar.config(state="disabled")
        self.ensayo_panel.boton_finalizar.config(state="normal")     
        self.ensayo_panel.boton_get_conf.config(state="disabled")
        self.ensayo_panel.boton_crudo.config(state="disabled")
        self.btn_ayuda.config(state="disabled")
        self.ensayo_panel.bloquear_duracion(True)
        self.serial_panel.btn_disconnect.config(state="disabled")   
        self.serial_panel.bloquear(True)
        self.serial_panel.bloquear_desplegable(True)
        self.panel_histograma.bloquear(True)
        self.panel_histograma.habilitar_borrar(False)


    def finalizar_ensayo(self):
        """
        Callback del botón 'Finalizar ensayo'.
        Solo envía la señal de stop; el cierre real y la normalización de la GUI
        lo hace _tick_ensayo cuando EnsayoSession confirma que terminó (has_finished).
        """
        if not self.ensayo or not self.ensayo.is_running():
            return

        self._stop_solicitado = True
        self.ensayo.stop()
        self.ensayo_panel.boton_finalizar.config(state="disabled")   # No se puede presionar de nuevo


    # ==================================================
    # Consultar configuración TAR
    # ==================================================
    def _mostrar_configuracion(self):
        """
        Callback del botón 'Consultar configuración TAR'.
        Si hay configuración disponible abre el popup; si no, muestra advertencia.
        """
        conf = self.ensayo.get_last_conf_struct()
        if conf is None:
            messagebox.showwarning(
                "Configuración TAR",
                "No hay una configuración asociada a este binario."
            )
            return

        self._popup_configuracion(conf)


    # ==================================================
    # Loop periódico
    # ==================================================
    def _tick_ensayo(self):
        """
        Se ejecuta cada 200 ms (via self.after al final del método).
        Es el "corazón" de la app: avanza la lógica del ensayo, actualiza
        los gráficos, muestra feedback temporal, y detecta el fin.

        Secciones:
            1. Avanza ensayo_sesion (guardado incremental, cierre pendiente).
            2. Detecta fin del ensayo y normaliza la GUI.
            3. Tick del histograma (dibuja datos nuevos).
            4. Feedback REPLAY (segundos restantes).
            5. Feedback LIVE (segundos transcurridos).
            6. Timeout LIVE (autostop si la duración se cumplió).
        """
        """
            Loop periódico. Ahora ignora replay en proceso.
        """
        # NUEVO: Saltar si estamos en replay bloqueante
        if hasattr(self, '_replay_en_proceso') and self._replay_en_proceso:
            self.after(200, self._tick_ensayo)
            return
    
        if self.ensayo and self._ensayo_activo:
            self.ensayo.tick()

            if self.ensayo.has_finished():
                self._on_ensayo_finalizado()

            self.panel_histograma.tick()

            # Feedback REPLAY solo si NO es bloqueante
            if (
                self.ensayo.is_running()
                and self.ensayo._mode is TARMode.REPLAY
                and self.fuente
                and not self._replay_en_proceso  # NUEVO
            ):
                porctj = self.fuente.get_progress_percentage()        
                self.ensayo_panel.set_estado(
                    f" - Procesando Histórico ({porctj} %) - "
                )

            # Feedback LIVE: muestra cuántos segundos transcurrieron desde el inicio.
            # Si hay duración finita, el contador se tapa en el máximo para que no
            # muestre un número mayor que el pedido mientras espera el cierre.
            if self.ensayo.is_running() and self.ensayo._mode is TARMode.LIVE:
                if self._t_inicio_live:
                    transcurrido = int(time.time() - self._t_inicio_live)

                    if self._temp and transcurrido >= self.duracion_total:
                        transcurrido = self.duracion_total   # Tapa en el máximo

                    self.ensayo_panel.var_estado.set(
                        f" - Ensayo en curso ({transcurrido} s) - "
                    )

            # Timeout LIVE para solicitar stop UNA SOLA VEZ.
            # _stop_solicitado evita que este bloque y el botón Finalizar manden
            # dos STOPs al TAR si el usuario presiona justo cuando se cumple el tiempo.
            if (
                self.ensayo._mode is TARMode.LIVE
                and self._temp
                and not self._stop_solicitado
                and time.time() >= self._temp
            ):
                self._stop_solicitado = True
                self.ensayo.stop()

        # Programa la siguiente ejecución de este método (loop no bloqueante)
        self.after(200, self._tick_ensayo)


    # ==================================================
    # Helpers de GUI
    # ==================================================
    def _aplicar_parametros(self, params):
        """
        Callback de PanelParametros cuando el usuario presiona Aplicar.
        Solo guarda los parámetros en memoria; se envían al TAR cuando
        el ensayo inicia (en iniciar_ensayo), no acá.
        """
        self._params_histeresis = params
        print("[MainWindow] Parámetros de histéresis almacenados:", params)


    def _validar_inicio(self):
        """
        Callback de PanelEnsayo antes de iniciar: verifica precondiciones.
        Retorna (True, "") si todo OK, o (False, "mensaje de error") si no.
        """
        if not self.fuente:
            return False, "No hay fuente de datos seleccionada"

        if not self.param_panel.parametros_estan_aplicados():
            return False, "Debe aplicar los umbrales antes de iniciar el ensayo"

        return True, ""


    def on_close(self):
        """Cierre de la ventana: detiene cualquier ensayo activo antes de destruir."""
        if self.ensayo and self.ensayo.is_running():
            self.ensayo.stop()
        self.destroy()


    def _reset_estado_ensayo(self):
        """Se ejecuta 1.2 segundos después del fin del ensayo para volver al texto neutral."""
        if not self.ensayo or not self.ensayo.is_running():
            self.ensayo_panel.var_estado.set(" - Listo para Ensayar - ")


    def _on_ensayo_finalizado(self):
        """
        Se ejecuta UNA VEZ cuando has_finished() retorna True.
        Responsabilidades:
            - Resetear todos los flags de estado.
            - Desbloquear toda la GUI.
            - Habilitar / deshabilitar botones según el modo que terminó.
            - Programar el reset del texto de estado después de 1.2 seg.
        """
        if not self._ensayo_activo:
            return   # Ya se ejecutó, no hacer doble

        # ── Reset de flags ───────────────────────────────────────────
        self._ensayo_activo   = False
        self._temp            = None
        self._stop_solicitado = False

        # ── Desbloqueo general ───────────────────────────────────────
        self.ensayo_panel.boton_iniciar.config(state="normal")
        self.ensayo_panel.boton_finalizar.config(state="disabled")
        self.ensayo_panel.boton_crudo.config(state="normal")
        self.btn_ayuda.config(state="normal")
        self.ensayo_panel.bloquear_duracion(False)

        self.param_panel.bloquear(False)

        self.panel_histograma.bloquear(False)
        self.panel_histograma.habilitar_borrar(True)

        self.serial_panel.bloquear_desplegable(False)
        self.serial_panel.btn_disconnect.config(state="normal")

        # ── Feedback específico por modo ─────────────────────────────
        if self.ensayo._mode is TARMode.REPLAY:
            self.ensayo_panel.var_estado.set(" - Binario procesado - ")
            # En replay el botón Consultar configuración se habilita (luego de finalizar el replay)
            # porque puede haber un test-config.txt asociado al archivo
            self.ensayo_panel.boton_get_conf.config(state="normal")
        else:
            self.ensayo_panel.var_estado.set(" - Ensayo finalizado - ")
            # En live no se habilita porque GET_CONF ya se hizo durante el cierre
            self.ensayo_panel.boton_get_conf.config(state="disabled")

        # Vuelve al texto "Listo para Ensayar" después de 1.2 segundos
        self.after(1200, self._reset_estado_ensayo)


