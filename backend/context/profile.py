"""JSON-shaped profile differences; lists are replaced as a single value."""
def profile_diff(before, after):
    added, updated, deleted = {}, {}, {}

    def visit(old, new, prefix=''):
        for key in sorted(old.keys() | new.keys()):
            path = f'{prefix}.{key}' if prefix else key
            if key not in old:
                added[path] = new[key]
            elif key not in new:
                deleted[path] = old[key]
            elif isinstance(old[key], dict) and isinstance(new[key], dict):
                visit(old[key], new[key], path)
            elif old[key] != new[key]:
                updated[path] = {'before': old[key], 'after': new[key]}

    visit(before, after)
    return {'added': added, 'updated': updated, 'deleted': deleted}
