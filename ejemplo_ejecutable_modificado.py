#!/usr/bin/env python3
"""
ejemplo_ejecutable_modificado.py
Archivo Python ejecutable modificado con logging y soporte de idiomas.
"""

import argparse
import datetime
import logging


def saludar(nombre: str, veces: int, idioma: str) -> None:
    """Imprime un saludo personalizado 'veces' veces en el idioma especificado."""
    for i in range(1, veces + 1):
        if idioma == "en":
            greeting = f"Hello, {nombre}! Welcome to this executable example."
        else:
            greeting = f"Hola, {nombre}! Bienvenido a este ejemplo ejecutable."
        print(f"[{i}] {greeting}")


def despedida(idioma: str) -> None:
    """Imprime una despedida en el idioma especificado."""
    if idioma == "en":
        farewell = "Goodbye!"
    else:
        farewell = "¡Adiós!"
    print(farewell)


def resumen() -> None:
    """Muestra informacion basica de ejecucion."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("-" * 40)
    print(f"Ejecutado el: {now}")
    print("Script de ejemplo finalizado correctamente.")
    print("-" * 40)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Ejemplo de script Python ejecutable modificado")
    parser.add_argument("--nombre", type=str, default="Mundo", help="Nombre para saludar")
    parser.add_argument("--veces", type=int, default=1, help="Numero de saludos")
    parser.add_argument("--idioma", type=str, choices=["es", "en"], default="es", help="Idioma del saludo (es/en)")
    args = parser.parse_args()

    logging.info("Iniciando ejecución del script modificado")
    saludar(args.nombre, args.veces, args.idioma)
    resumen()
    despedida(args.idioma)


if __name__ == "__main__":
    main()
