"""General town contact: a directory response without starting research work."""
import tomllib


def directory(town, *, public=False):
    residents = []
    for path in sorted((town.city_root / 'agents').glob('*/agent.toml')):
        if public and path.parent.name not in {'q', 'bloodninja', 'irb', 'dac'}:
            continue
        try:
            settings = tomllib.loads(path.read_text())
        except (OSError, ValueError):
            continue
        residents.append({'name': path.parent.name, 'role': str(settings.get('description', 'Town resident'))})
    purpose = ('Credential applications and their review by the town operator.' if town.kind == 'authority'
               else f'{town.population} pangenome resources and bounded research queries.')
    return {'ok': True, 'resident': 'contact', 'mode': 'directory', 'town': town.name,
            'residents': residents,
            'text': f"Welcome to {town.display}. {purpose} "
                    'I am the automated general contact. Choose a named resident for a conversation. '
                    'Available residents: ' + (', '.join(r['name'] for r in residents) or 'none currently configured') +
                    '. Use resources to list published files and describe for town services.'}
