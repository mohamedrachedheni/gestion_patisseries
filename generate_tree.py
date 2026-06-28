# fichier pour tracer l'arboressanse des répertoires et des fichers du projet
# commande utilisée dans cmd : (venv) C:\Users\Heni\Projet Patisseries\div\patisserie_management>python generate_tree.py


import os
from pathlib import Path

def generate_tree(startpath=".", exclude_dirs=None, output_file="arborescence_projet.txt"):
    if exclude_dirs is None:
        exclude_dirs = ['venv', '__pycache__', '.git', '.pytest_cache', 
                       'migrations', '.vscode', 'node_modules', '.idea', 'env']
    
    exclude_extensions = ['.pyc', '.pyo', '.pyd', '.DS_Store', '.log']
    
    def should_exclude(path):
        name = os.path.basename(path)
        # Exclure les dossiers
        if os.path.isdir(path) and name in exclude_dirs:
            return True
        # Exclure les fichiers par extension
        if os.path.isfile(path):
            for ext in exclude_extensions:
                if name.endswith(ext):
                    return True
        return False
    
    def walk_dir(dir_path, prefix=""):
        lines = []
        items = []
        
        try:
            for item in sorted(os.listdir(dir_path)):
                full_path = os.path.join(dir_path, item)
                if should_exclude(full_path):
                    continue
                items.append((item, full_path, os.path.isdir(full_path)))
        except PermissionError:
            return lines
        
        for i, (item, full_path, is_dir) in enumerate(items):
            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            new_prefix = prefix + ("    " if is_last else "│   ")
            
            if is_dir:
                lines.append(f"{prefix}{connector}{item}/")
                lines.extend(walk_dir(full_path, new_prefix))
            else:
                # Afficher la taille
                try:
                    size = os.path.getsize(full_path)
                    if size < 1024:
                        size_str = f" ({size} o)"
                    elif size < 1024*1024:
                        size_str = f" ({size/1024:.1f} Ko)"
                    else:
                        size_str = f" ({size/(1024*1024):.1f} Mo)"
                except:
                    size_str = ""
                lines.append(f"{prefix}{connector}{item}{size_str}")
        
        return lines
    
    # Écrire dans le fichier
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("=" * 80 + "\n")
        f.write("ARBORESCENCE DU PROJET\n")
        f.write("=" * 80 + "\n\n")
        
        project_name = os.path.basename(os.path.abspath(startpath))
        f.write(f"{project_name}/\n")
        
        lines = walk_dir(startpath)
        f.write("\n".join(lines))
        
        f.write("\n\n" + "=" * 80 + "\n")
        f.write(f"Résumé:\n")
        f.write(f"  - Dossiers: {len([l for l in lines if '/' in l])}\n")
        f.write(f"  - Fichiers: {len([l for l in lines if '/' not in l])}\n")
    
    print(f"✅ Arborescence générée dans: {output_file}")
    return output_file

if __name__ == "__main__":
    # Générer l'arborescence
    generate_tree(".")