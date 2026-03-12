
# gui/Panel_Serial.py
"""
Panel de conexión serie.

Responsabilidad ÚNICA: mostrar puertos disponibles, permitir conectar /
desconectar, y notificar a MainWindow cuando el usuario hace algo.
Este panel NO habla con el hardware directamente: todo evento pasa por
callbacks que MainWindow proporcionó al construirlo.

Patrón usado en todos los paneles GUI:
    Panel captura evento del usuario
    llama al callback que MainWindow le dio
    MainWindow ejecuta la lógica real
    MainWindow actualiza el panel llamando a set_conectado / set_desconectado
"""

import tkinter as tk
from tkinter import ttk
import serial.tools.list_ports      # Dependencia única de PySerial, y solo para enumerar dispositivos.
import platform


class SerialPanel(ttk.LabelFrame):

    def __init__(self, parent, on_connect_callback, on_disconnect_callback):
        super().__init__(parent, text="Puertos", padding=0)

        # Callbacks proporcionados por MainWindow al construir este panel.
        # on_connect  recibe el nombre del puerto (str) cuando el usuario presiona Conectar.
        # on_disconnect no recibe argumento: MainWindow sabe qué puerto cerrar.
        self.on_connect    = on_connect_callback
        self.on_disconnect = on_disconnect_callback

        # ---------------------------------------------------------
        # Título
        # ---------------------------------------------------------
        ttk.Label(
            self,
            text="Conexión Serie/USB",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(pady=(0, 5))

        # ---------------------------------------------------------
        # Desplegable de puertos + botón refresh
        # ---------------------------------------------------------
        ports_frame = ttk.Frame(self)
        ports_frame.pack(fill="x")

        ttk.Label(ports_frame, text="Puerto COM:").pack(side="left")

        # port_var almacena el puerto seleccionado en la lista.
        # state="readonly" impide que el usuario escriba un nombre inventado:
        # solo puede elegir de los que el sistema reportó.
        self.port_var = tk.StringVar()
        self.combo_ports = ttk.Combobox(
            ports_frame,
            textvariable=self.port_var,
            state="readonly",
            width=20
        )
        self.combo_ports.pack(side="left", padx=5)

        # El botón ↻ reescanea los puertos físicos sin reiniciar la app.
        # Útil cuando se conecta un adaptador USB después de arrancar.
        self.btn_refresh = ttk.Button(
            ports_frame,
            text="↻",
            width=4,
            command=self.refresh_ports
        )
        self.btn_refresh.pack(side="left")

        # ---------------------------------------------------------
        # Botones conectar / desconectar
        # ---------------------------------------------------------
        btns_frame = ttk.Frame(self)
        btns_frame.pack(pady=10)

        self.btn_connect = ttk.Button(
            btns_frame,
            text="Conectar",
            command=self._request_connect
        )
        self.btn_connect.pack(side="left", padx=5)

        # Desconectar empieza deshabilitado porque no hay conexión activa aún.
        self.btn_disconnect = ttk.Button(
            btns_frame,
            text="Desconectar",
            state="disabled",
            command=self._request_disconnect
        )
        self.btn_disconnect.pack(side="left", padx=5)

        # ---------------------------------------------------------
        # Etiqueta de estado visual
        # ---------------------------------------------------------
        self.status_var = tk.StringVar(value=" - Puerto Desconectado - ")
        ttk.Label(
            self,
            textvariable=self.status_var,
            font=("JetBrains Mono", 11),
            width=37,
            anchor="w"
        ).pack(padx=20, pady=5)

        # Carga la lista de puertos al construir el panel (primera vez).
        self.refresh_ports()


    # =========================================================================
    # Escaneo de puertos físicos
    # =========================================================================
    def refresh_ports(self):
        """
        Reescanea los puertos serie del sistema y actualiza el desplegable.
        
        Compatibilidad multiplataforma:
        - Windows: Muestra todos los puertos COM
        - Linux: Filtra solo ttyUSB* y ttyACM* (dispositivos USB/Arduino)
        
        Si el puerto que estaba seleccionado sigue existiendo, lo mantiene.
        Si desapareció, selecciona el primero disponible.
        """
        ports = serial.tools.list_ports.comports()
        ports_list = [p.device for p in ports]
        
        # Filtrado específico para Linux 
        if platform.system() == "Linux":
            # Filtrar solo dispositivos USB relevantes
            # ttyUSB*: Adaptadores USB-Serial genéricos
            # ttyACM*: Arduino, módems CDC ACM
            ports_list = [
                p for p in ports_list 
                if "ttyUSB" in p or "ttyACM" in p
            ]
        
        # Si no hay puertos disponibles, mostrar mensaje
        if not ports_list:
            ports_list = ["No hay puertos disponibles"]
        
        current = self.port_var.get()  # Puerto seleccionado antes del rescan
        
        self.combo_ports["values"] = ports_list
        
        # Mantener selección previa si sigue disponible
        if current in ports_list:
            self.port_var.set(current)
        elif ports_list[0] != "No hay puertos disponibles":
            self.combo_ports.current(0)
        else:
            self.port_var.set(ports_list[0])  # Mensaje de "no disponibles"


    # =========================================================================
    # Eventos del usuario las notificaciones hacia MainWindow
    # =========================================================================
    def _request_connect(self):
        """El usuario presionó Conectar. Verifica que haya algo seleccionado y notifica."""
        port = self.port_var.get()
        if not port:
            self.status_var.set(" - No hay puerto seleccionado - ")
            return

        if self.on_connect:
            self.on_connect(port)   # MainWindow va a abrir el puerto y llamar a set_conectado()


    def _request_disconnect(self):
        """El usuario presionó Desconectar. Notifica a MainWindow."""
        if self.on_disconnect:
            self.on_disconnect()    # MainWindow va a cerrar el puerto y llamar a set_desconectado()


    # =========================================================================
    # API de estado: MainWindow a Panel (actualizar lo que se muestra)
    # =========================================================================
    def set_conectado(self, port):
        """MainWindow llama esto cuando la conexión fue exitosa."""
        self.status_var.set(f" - Puerto Conectado a {port} - ")
        self.btn_connect.config(state="disabled")       # Ya conectado: no tiene sentido conectar de nuevo
        self.btn_disconnect.config(state="normal")      # Ahora sí se puede desconectar


    def set_desconectado(self):
        """MainWindow llama esto cuando se cerró la conexión (normal o por error)."""
        self.status_var.set(" - Puerto Desconectado - ")
        self.btn_connect.config(state="normal")         # Vuelve a estar disponible
        self.btn_disconnect.config(state="disabled")    # Nada que desconectar


    # =========================================================================
    # Bloqueo durante ensayo
    # =========================================================================
    def bloquear(self, flag: bool):
        """
        Durante un ensayo activo no se debe cambiar la conexión serie.
        MainWindow activa este bloqueo al iniciar y lo levanta al finalizar.
        """
        state = "disabled" if flag else "normal"
        self.btn_connect.config(state=state)
        if not flag:
            # Al desbloquear, desconectar vuelve a estar deshabilitado porque
            # el estado "conectado/desconectado" lo maneja set_conectado / set_desconectado,
            # no este método.
            self.btn_disconnect.config(state="disabled")


    def bloquear_desplegable(self, flag: bool):
        """
        Bloquea el desplegable y el botón refresh durante el ensayo.
        No tiene sentido cambiar de puerto mientras se están leyendo datos.
        """
        state = "disabled" if flag else "normal"
        self.combo_ports.config(state=state)
        self.btn_refresh.config(state=state)


    # =========================================================================
    # API de consulta
    # Actualmente no se usa desde MainWindow, pero vale tenerla: si en el futuro
    # algún otro panel necesita saber qué puerto está activo sin ir a MainWindow,
    # esta es la entrada.
    # =========================================================================
    def get_port(self) -> str:
        """Retorna el nombre del puerto actualmente seleccionado en el desplegable."""
        return self.port_var.get()

