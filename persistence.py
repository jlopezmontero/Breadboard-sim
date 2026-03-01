"""
Save/Load board state as .bbsim (JSON) files.
"""

import json
from board import Board, PlacedComponent, GuideLine, DivisionLine, TextLabel


FORMAT_VERSION = 1


def save_board(board, filepath, library):
    """Save board and all placed components to a .bbsim JSON file."""
    data = {
        'version': FORMAT_VERSION,
        'board': board.to_dict(),
        'components': [pc.to_dict() for pc in board.components.values()],
        'guides': [gl.to_dict() for gl in board.guides],
        'divisions': [dl.to_dict() for dl in board.divisions],
        'text_labels': [tl.to_dict() for tl in board.text_labels],
    }
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_board(filepath, library):
    """Load board from a .bbsim JSON file. Returns Board or raises."""
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    version = data.get('version', 1)
    if version > FORMAT_VERSION:
        raise ValueError(f"File version {version} is newer than supported ({FORMAT_VERSION})")

    board_data = data['board']
    board = Board.from_dict(board_data)

    for comp_data in data.get('components', []):
        type_id = comp_data['type']
        comp_def = library.get(type_id)
        if comp_def is None:
            continue  # Skip unknown component types
        comp_id = comp_data['id']
        anchor_row = comp_data['anchor_row']
        anchor_col = comp_data['anchor_col']
        rotation = comp_data.get('rotation', 0)
        pc = board.place_component(comp_def, anchor_row, anchor_col, rotation, comp_id=comp_id)
        if pc:
            if comp_data.get('label'):
                pc.label = comp_data['label']
            if comp_data.get('label_size') is not None:
                pc.label_size = comp_data['label_size']
            if comp_data.get('label_align'):
                pc.label_align = comp_data['label_align']

    for gl_data in data.get('guides', []):
        board.guides.append(GuideLine.from_dict(gl_data))

    for dl_data in data.get('divisions', []):
        board.divisions.append(DivisionLine.from_dict(dl_data))

    for tl_data in data.get('text_labels', []):
        tl = TextLabel.from_dict(tl_data)
        board.text_labels.append(tl)
    # Sync counter so new labels don't collide with loaded ones
    if board.text_labels:
        nums = []
        for tl in board.text_labels:
            try:
                nums.append(int(tl.id.lstrip('T')))
            except ValueError:
                pass
        if nums:
            board._next_text_label_num = max(nums) + 1

    return board
