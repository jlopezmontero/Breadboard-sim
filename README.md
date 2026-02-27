# Breadboard Simulator

Herramienta de visualizacion y planificacion de layouts en perfboard (placa perforada con pads independientes en reticula X*Y). Sub-proyecto de SBC-WALL, pero sirve para cualquier proyecto.

![Breadboard Simulator](screenshot.png)

## Requisitos

- Python 3.8+
- tkinter (incluido en la distribucion estandar de Python)
- **Cero dependencias externas**

## Uso

```bash
python main.py              # Board nuevo (SBC-WALL 57x74 por defecto)
python main.py layout.bbsim # Abrir archivo existente
```

## Controles

| Tecla/Raton | Accion |
|------------|--------|
| Click izq. | Seleccionar / Colocar / Borrar (segun modo) |
| Click medio / derecho + arrastrar | Pan (desplazar vista) |
| Mousewheel | Zoom centrado en cursor |
| **R** | Rotar componente (90 CW) |
| **Del** | Borrar componente seleccionado |
| **Esc** | Cancelar operacion / deseleccionar |
| **F** | Zoom fit (ajustar al board) |
| **+/-** | Zoom in/out |
| **Ctrl+Z** / **Ctrl+Y** | Undo / Redo |
| **Ctrl+S** | Guardar |
| **Ctrl+O** | Abrir |
| **Ctrl+N** | Nuevo board |
| **Ctrl+Q** | Salir |

## Modos de interaccion

- **Select**: Click para seleccionar, arrastrar para mover.
- **Place**: Seleccionar componente de la palette, click para colocar. Ghost verde = valido, rojo = colision.
- **Delete**: Click sobre componente para eliminarlo.

## Formato de archivo (.bbsim)

JSON con version, dimensiones del board, y array de componentes colocados. Extensible con componentes custom definidos en `library/*.json`.

## Estructura

```
main.py              # Entry point
board.py             # Modelo: grid de pads, colocacion, colisiones
components.py        # Geometria de componentes (DIP, axial, radial, headers)
component_library.py # Catalogo built-in + carga de JSON externos
renderer.py          # Renderizado Canvas: pads, componentes, ghost, zoom
gui.py               # Ventana principal: palette, toolbar, menus, modos
persistence.py       # Save/Load JSON (.bbsim)
library/             # Biblioteca extensible (JSON)
```

## Presets de board

| Preset | Dimensiones |
|--------|------------|
| 50x70mm | 20x28 pads |
| 70x90mm | 28x36 pads |
| SBC-WALL | 57x74 pads (default) |
| Custom | Definido por usuario (5-200) |
