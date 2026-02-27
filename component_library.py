"""
Component library: built-in catalog + loading from JSON files.
"""

import json
import os
from components import BUILTIN_COMPONENTS, ComponentDef


class ComponentLibrary:
    """Manages the component catalog, including user-defined JSON components."""

    # Categories for palette display
    CATEGORIES = {
        'DIP ICs': ['DIP-8', 'DIP-14', 'DIP-16', 'DIP-20', 'DIP-24',
                     'DIP-28', 'DIP-28W', 'DIP-40W'],
        'Passives': ['AXIAL-4', 'AXIAL-5', 'RADIAL-2', 'ELEC-CAP', 'HC49'],
        'Semiconductors': ['LED', 'LED'],  # handled by name below
        'Connectors': [],  # filled dynamically
    }

    def __init__(self):
        self.components = dict(BUILTIN_COMPONENTS)
        self._build_categories()

    def _build_categories(self):
        """Rebuild category lists from current components."""
        self._categories = {}

        # DIP ICs
        dips = sorted(
            [c for c in self.components.values() if c.type_id.startswith('DIP-')],
            key=lambda c: (len(c.pins), c.type_id)
        )
        self._categories['DIP ICs'] = dips

        # Passives
        passives = [c for c in self.components.values()
                    if c.type_id.startswith(('AXIAL-', 'RADIAL-', 'CAP-', 'ELEC-', 'HC49', 'SIP-'))]
        self._categories['Passives'] = passives

        # Semiconductors
        semis = [c for c in self.components.values()
                 if c.type_id.startswith(('LED', 'TO-', 'DIODE'))]
        self._categories['Semiconductors'] = semis

        # Modules / breakout boards
        mods = [c for c in self.components.values()
                if c.type_id.startswith('MOD-')]
        if mods:
            self._categories['Modules'] = mods

        # Connectors
        hdrs = sorted(
            [c for c in self.components.values() if c.type_id.startswith('HDR-')],
            key=lambda c: len(c.pins)
        )
        self._categories['Connectors'] = hdrs

        # User/JSON (anything not in above categories)
        known_ids = set()
        for cat_list in self._categories.values():
            for comp in cat_list:
                known_ids.add(comp.type_id)
        others = [c for c in self.components.values() if c.type_id not in known_ids]
        if others:
            self._categories['Custom'] = others

    def get_categories(self):
        """Return dict of category_name -> [ComponentDef]."""
        return self._categories

    def get(self, type_id):
        """Get ComponentDef by type_id."""
        return self.components.get(type_id)

    def load_json_dir(self, dirpath):
        """Load all .json files from a directory."""
        if not os.path.isdir(dirpath):
            return
        for fname in sorted(os.listdir(dirpath)):
            if fname.endswith('.json'):
                self.load_json_file(os.path.join(dirpath, fname))
        self._build_categories()

    def load_json_file(self, filepath):
        """Load components from a JSON file."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        if isinstance(data, list):
            for entry in data:
                self._load_json_component(entry)
        elif isinstance(data, dict) and 'components' in data:
            for entry in data['components']:
                self._load_json_component(entry)

    def _load_json_component(self, entry):
        """Parse a single component from JSON."""
        try:
            comp = ComponentDef(
                type_id=entry['type_id'],
                name=entry.get('name', entry['type_id']),
                ref_prefix=entry.get('ref_prefix', 'X'),
                pins=[tuple(p) for p in entry['pins']],
                body_cells=[tuple(b) for b in entry.get('body_cells', [])],
                color=entry.get('color', '#808080'),
                pin_labels=entry.get('pin_labels'),
            )
            self.components[comp.type_id] = comp
        except (KeyError, TypeError):
            pass
