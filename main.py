
from gui.Ventana_gui import MainWindow

# Asegurarse de que el usuario tenga permisos para acceder al puerto serie
# sudo usermod -a -G dialout $USER
# usermod -a -G dialout $SUDO_USER || true  (NO usar)

if __name__ == "__main__":
    app = MainWindow()
    app.mainloop()