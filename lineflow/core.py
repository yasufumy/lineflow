from typing import Sequence, Any, Union, Callable, List, Tuple, Iterator, Iterable
import pickle
import copy
from pathlib import Path
from itertools import accumulate, chain, islice
from collections import deque
import bisect


class IndexedMixin:
    def __getitem__(self, index: Union[int, slice]) -> Union[Any, List[Any]]:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return [self.get_example(i) for i in range(start, stop, step)]

        if index >= 0:
            if index >= len(self):
                raise IndexError(f'{self.__class__.__name__} object index out of range')
        else:
            if index < - len(self):
                raise IndexError(f'{self.__class__.__name__} object index out of range')
            index += len(self)

        return self.get_example(index)

    def get_example(self, i: int) -> Any:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError


class IndexedConcat(IndexedMixin):
    def __init__(self, *datasets: List[Sequence[Any]]) -> None:
        self._datasets = datasets
        self._offsets = None
        self._length = None
        self._ready = False

    def _prepare(self) -> None:
        if self._ready:
            return
        self._lengths = list(accumulate(len(d) for d in self._datasets))
        self._offsets = [0] + self._lengths[:-1]
        self._length = self._lengths[-1]
        self._ready = True

    def __iter__(self) -> Iterator[Any]:
        for d in self._datasets:
            yield from d

    def __getitem__(self, index: Union[int, slice]) -> Union[Any, List[Any]]:
        self._prepare()
        return super().__getitem__(index)

    def get_example(self, i: int) -> Any:
        j = bisect.bisect_right(self._lengths, i)
        return self._datasets[j][i - self._offsets[j]]

    def __len__(self) -> int:
        self._prepare()
        return self._length


class IndexedZip(IndexedMixin):
    def __init__(self, *datasets: List[Sequence[Any]]) -> None:
        self._datasets = datasets
        self._length = None

    def __iter__(self) -> Iterator[Tuple[Any]]:
        yield from zip(*self._datasets)

    def get_example(self, i: int) -> Tuple[Any]:
        return tuple(d[i] for d in self._datasets)

    def __len__(self) -> int:
        if self._length is None:
            self._length = min(len(d) for d in self._datasets)
        return self._length


class Dataset(IndexedMixin):
    def __init__(self,
                 dataset: Sequence[Any]) -> None:
        self._dataset = dataset
        self._length = None

    def __iter__(self) -> Iterator[Any]:
        yield from self._dataset

    def __len__(self) -> int:
        if self._length is None:
            self._length = self.get_length()
        return self._length

    def __add__(self, other: 'Dataset') -> 'ConcatDataset':
        return ConcatDataset(self, other)

    def get_example(self, i: int) -> Any:
        return self._dataset[i]

    def get_length(self) -> int:
        return len(self._dataset)

    def map(self, map_func: Callable[[Any], Any]) -> 'MapDataset':
        return MapDataset(self, map_func)

    def all(self) -> List[Any]:
        return list(self)

    def take(self, n: int) -> List[Any]:
        return list(islice(self, n))

    def first(self) -> Any:
        return next(iter(self))

    def save(self, filename: str) -> 'CacheDataset':
        path = Path(filename)
        if path.exists():
            print(f'Loading data from {filename}...')
            with path.open('rb') as f:
                cache = pickle.load(f)
        else:
            if not path.parent.exists():
                path.parent.mkdir(parents=True)
            print(f'Saving data to {filename}...')
            cache = list(self)
            with path.open('wb') as f:
                pickle.dump(cache, f)
        return CacheDataset(self, cache)


class ConcatDataset(Dataset):
    def __init__(self, *datasets: List[Dataset]) -> None:
        assert all(isinstance(d, Dataset) for d in datasets)

        super().__init__(IndexedConcat(*datasets))


class ZipDataset(Dataset):
    def __init__(self, *datasets: List[Dataset]) -> None:
        assert all(isinstance(d, Dataset) for d in datasets)

        super().__init__(IndexedZip(*datasets))


class MapDataset(Dataset):
    def __init__(self,
                 dataset: Dataset,
                 map_func: Callable[[Any], Any]) -> None:
        assert callable(map_func)

        if isinstance(dataset, MapDataset):
            funcs = copy.deepcopy(dataset._funcs)
            funcs.append(map_func)
            processed_funcs = copy.deepcopy(dataset._processed_funcs)
        else:
            funcs = [map_func]
            processed_funcs = []

        self._funcs = funcs
        self._processed_funcs = processed_funcs

        if isinstance(dataset, Dataset):
            dataset = dataset._dataset

        super().__init__(dataset)

    def __iter__(self) -> Iterator[Any]:
        for x in self._dataset:
            for f in self._funcs:
                x = f(x)
            yield x

    def get_example(self, i: int) -> Any:
        x = self._dataset[i]
        for f in self._funcs:
            x = f(x)
        return x


class CacheDataset(MapDataset):
    def __init__(self,
                 dataset: Dataset,
                 cache: List[Any]) -> None:
        if isinstance(dataset, MapDataset):
            funcs = copy.deepcopy(dataset._funcs)
            processed_funcs = funcs + copy.deepcopy(dataset._processed_funcs)
        else:
            processed_funcs = []

        self._funcs = []
        self._processed_funcs = processed_funcs
        self._cache = cache
        self._length = len(self._cache)

        super(MapDataset, self).__init__(cache)


def lineflow_concat(*datasets: List[Dataset]) -> ConcatDataset:
    return ConcatDataset(*datasets)


def lineflow_zip(*datasets: List[Dataset]) -> ZipDataset:
    return ZipDataset(*datasets)


def lineflow_filter(
        predicate: Callable[[Any], bool],
        dataset: Dataset,
        lazy: bool = False) -> Union[Iterator[Any], List[Any]]:
    iterator = filter(predicate, dataset)
    if lazy:
        return iterator
    else:
        return list(iterator)


def lineflow_flat_map(
        map_func: Callable[[Iterable[Any]], Any],
        dataset: Dataset,
        lazy: bool = False) -> Union[Iterator[Any], List[Any]]:
    iterator = chain.from_iterable(map(map_func, dataset))
    if lazy:
        return iterator
    else:
        return list(iterator)


def lineflow_window(
        dataset: Dataset,
        window_size: int,
        shift: int = None,
        lazy: bool = False) -> Union[Iterator[Any], List[Any]]:
    shift = shift or window_size

    def generator():
        iterator = iter(dataset)
        window = deque([], window_size)
        append = window.append

        for _ in range(window_size):
            append(next(iterator))
        yield tuple(window)

        i = 0
        for x in iterator:
            append(x)
            i = (i + 1) % shift
            if i % shift == 0:
                yield tuple(window)

        if (i % shift) and (shift - i < window_size):
            popleft = window.popleft
            for _ in range(shift - i):
                popleft()
        yield tuple(window)

    iterator = generator()
    if lazy:
        return iterator
    else:
        return list(iterator)


def lineflow_load(filename: str) -> Dataset:
    print(f'Loading data from {filename}...')
    with open(filename, 'rb') as f:
        dataset = pickle.load(f)
    return Dataset(dataset)
