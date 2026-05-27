"""
基数排序（Radix Sort）实现
支持正整数和负整数排序
"""


def radix_sort(arr):
    """
    基数排序（支持正负数）
    :param arr: 待排序列表
    :return: 排序后的新列表
    """
    if not arr:
        return arr

    # 分离正负数
    negatives = [x for x in arr if x < 0]
    positives = [x for x in arr if x >= 0]

    # 负数取绝对值排序后反转，正数正常排序
    if negatives:
        negatives = _radix_sort_positive([-x for x in negatives])
        negatives = [-x for x in reversed(negatives)]

    if positives:
        positives = _radix_sort_positive(positives)

    return negatives + positives


def _radix_sort_positive(arr):
    """
    基数排序核心实现（仅处理非负整数）
    :param arr: 非负整数列表
    :return: 排序后的新列表
    """
    if not arr:
        return arr

    max_val = max(arr)
    exp = 1  # 当前位数：1=个位，10=十位，100=百位 ...

    while max_val // exp > 0:
        arr = _counting_sort_by_digit(arr, exp)
        exp *= 10

    return arr


def _counting_sort_by_digit(arr, exp):
    """
    按指定位数进行计数排序
    :param arr: 待排序列表
    :param exp: 位数（1=个位，10=十位...）
    :return: 按该位排序后的列表
    """
    n = len(arr)
    output = [0] * n
    count = [0] * 10  # 0-9 共10个数字

    # 统计当前位每个数字的出现次数
    for num in arr:
        digit = (num // exp) % 10
        count[digit] += 1

    # 将计数转换为累积位置
    for i in range(1, 10):
        count[i] += count[i - 1]

    # 从后往前遍历，保证稳定性
    for i in range(n - 1, -1, -1):
        digit = (arr[i] // exp) % 10
        output[count[digit] - 1] = arr[i]
        count[digit] -= 1

    return output


if __name__ == "__main__":
    test = [170, 45, 75, 90, 802, 24, 2, 66]
    print("原始数组:", test)
    print("基数排序:", radix_sort(test))

    test2 = [-4, 1, -9, 12, 8, 0, -12, 3]
    print("\n含负数:", test2)
    print("基数排序:", radix_sort(test2))
