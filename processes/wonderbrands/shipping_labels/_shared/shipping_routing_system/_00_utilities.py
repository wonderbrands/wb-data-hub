import time as tm
import functools
import threading
import logging


dotenv_path = "/var/lib/jenkins/m1/.env"
#dotenv_path = r"C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\Tools\ML_odoo18\.env2"

class Utilities:

    # Lo pongo estaticos para hacerlos utilizables en otros modulos
    @staticmethod
    def measure_execution_time(func):
        """Decorador para medir el tiempo de ejecución de un método de una clase."""

        def wrapper(self, *args, **kwargs):
            start = tm.time()
            result = func(self, *args, **kwargs)
            end = tm.time()
            print(f"Tiempo de respuesta de '{func.__name__}': {round(end - start, 2)} [sec]")

            return result

        return wrapper

    @staticmethod
    def trace_thread(func):
        """Decorador para rastrear el hilo que ejecuta la función y su orden."""
        call_order = []  # Lista para rastrear el orden de ejecución, tener control de las llamadas al m[etodo

        @functools.wraps(func)  #decorador para preservar los metadatos de la función decorada
        def wrapper(*args, **kwargs):
            thread_name = threading.current_thread().name
            order = len(call_order) + 1
            call_order.append(order)
            logging.info(f"[START] {func.__name__} | Thread: {thread_name} | Order: {order}")
            result = func(*args, **kwargs)
            logging.info(f"[END] {func.__name__} | Thread: {thread_name} | Order: {order}")
            return result

        return wrapper