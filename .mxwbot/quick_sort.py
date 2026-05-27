"""
快速排序（Quick Sort）实现
包含非原地版和原地版两种方式
"""


def quick_sort(arr):
    """
    快速排序（非原地版，返回新列表）
    :param arr: 待排序列表
    :return: 排序后的新列表
    """
    if len(arr) <= 1:
        return arr

    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    return quick_sort(left) + middle + quick_sort(right)


def quick_sort_inplace(arr, low=0, high=None):
    """
    原地快速排序（直接修改原列表，省内存）
    :param arr: 待排序列表
    :param low: 起始下标
    :param high: 结束下标
    """
    if high is None:
        high = len(arr) - 1

    if low < high:
        pi = partition(arr, low, high)
        quick_sort_inplace(arr, low, pi - 1)
        quick_sort_inplace(arr, pi + 1, high)


def partition(arr, low, high):
    """分区函数：以 arr[high] 为基准"""
    pivot = arr[high]
    i = low - 1

    for j in range(low, high):
        if arr[j] <= pivot:
            i += 1
            arr[i], arr[j] = arr[j], arr[i]

    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1


if __name__ == "__main__":
    test = [-4, 1, 9, -12, 8, 0, 12, 3]
    print("原始数组:", test)

    # 非原地版
    sorted_arr = quick_sort(test.copy())
    print("非原地快排:", sorted_arr)

    # 原地版
    arr_copy = test.copy()
    quick_sort_inplace(arr_copy)
    print("原地快排:", arr_copy)
