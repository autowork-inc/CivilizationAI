import os

def print_tree(start_path, prefix=""):
    """Рекурсивная функция для отображения дерева файлов"""
    
    # Получаем все элементы в директории
    try:
        items = os.listdir(start_path)
    except PermissionError:
        print(f"{prefix}[Доступ запрещен]")
        return
    
    # Сортируем: сначала папки, потом файлы
    items = sorted(items, key=lambda x: (not os.path.isdir(os.path.join(start_path, x)), x.lower()))
    
    for i, item in enumerate(items):
        # Пропускаем скрытые файлы
        if item.startswith('.'):
            continue
            
        item_path = os.path.join(start_path, item)
        is_last = i == len(items) - 1
        
        # Определяем префикс для текущего элемента
        connector = "└── " if is_last else "├── "
        print(f"{prefix}{connector}{item}")
        
        # Если это директория, рекурсивно обрабатываем её
        if os.path.isdir(item_path):
            extension = "    " if is_last else "│   "
            print_tree(item_path, prefix + extension)

if __name__ == "__main__":
    current_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"📁 Дерево файлов: {current_dir}")
    print(".")
    print_tree(current_dir)