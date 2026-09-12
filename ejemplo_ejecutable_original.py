#!/usr/bin/env python3
"""
ejemplo_ejecutable.py
Archivo Python ejecutable de ejemplo para visualizar en el editor Monaco (panel preview).
"""

import argparse
import datetime


def saludar(nombre: str, veces: int) -> None:
    """Imprime un saludo personalizado 'veces' veces."""
    for i in range(1, veces + 1):
        print(f"[{i}] Hola, {nombre}! Bienvenido a este ejemplo ejecutable.")


def resumen() -> None:
    """Muestra informacion basica de ejecucion."""
    ahora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("-" * 40)
    print(f"Ejecutado el: {ahora}")
    print("Script de ejemplo finalizado correctamente.")
    print("-" * 40)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ejemplo de script Python ejecutable")
    parser.add_argument("--nombre", type=str, default="Mundo", help="Nombre para saludar")
    parser.add_argument("--veces", type=int, default=1, help="Numero de saludos")
    args = parser.parse_args()

    saludar(args.nombre, args.veces)
    resumen()


if __name__ == "__main__":
    main()
