# No Human Allowed MMO — лёгкий движок + базовая физика (MVP)

Цель: **максимально лёгкий серверный движок** — тики + целочисленная физика, без непрерывной
симуляции/коллизий/3D. Концепция та же, что в [MECHANICS.md](MECHANICS.md), но сведена к минимуму,
который реально написать (~сотни строк) и крутить тысячами сущностей.

## A. Модель движка
- **Дискретное пространство:** сетка клеток (2D для MVP). Клетка держит объекты (лимит по слотам/массе).
  Локальность = соседние клетки (reach = 1). Никакой физики коллизий.
- **Дискретное время:** тики. На тик: собрать intents → валидировать → применить → прогнать
  behaviors компонентов → отдать observations. Детерминированный порядок (по `id`).
- **Состояние = простые данные:** сущности с атрибутами; in-memory dict / SQLite. Физ-движок не нужен.
- **Ресурсы = целые числа.** Сохранение = целочисленная арифметика (вход списали, выход начислили по
  коэффициенту; из ничего не растёт).
- **Энергия = единая целочисленная «валюта действий».** Любой intent стоит energy; нет energy — нет действия.
- **Авторитетность:** агент шлёт intents по API/MCP, физику применяет движок.

## B. Сущность (единая структура)
`{ id, type, pos:(x,y), ports[], links[], buffers:{resource:int}, behavior }`
- **port** = `{name, kind, dir}` — kind ∈ {power, fluid, item, signal, mount}; dir ∈ {in, out, bi}.
- **link** = соединение двух совместимых портов (kind совпадает, dir противоположны).
- **behavior** = крошечная функция «что делаю за тик, если входные порты удовлетворены».
- **Capability сборки** = объединение behaviors соединённых компонентов с удовлетворёнными портами.
  (Это и есть «вычислено из состава» — но дёшево: просто прогон списка behaviors.)

## C. Примитивы (атомы-ресурсы) — стартовый набор
Целые числа: `energy`, `ore`, `metal`, `fuel`, `water`, `stone`, `crystal`.
7 штук — «периодическая таблица» MVP, расширяемо.

## D. Компоненты (деталь = порты + behavior/тик)
- **Frame** (mount×4) — структура, держит сборку.
- **Wire** (power in/out) — передаёт energy между связанными.
- **Pipe** (fluid in/out) — передаёт water/fuel.
- **Container(R)** (item in/out) — буфер одного ресурса R.
- **Pump** (power in; fluid in→out) — гонит fluid, −1 energy/ед.
- **Drill** (power in; item out) — на OreDeposit: −5 energy → +1 ore.
- **Furnace** (power+fuel+ore in; metal out) — −5 energy −1 fuel −2 ore → +1 metal.
- **Generator** (fuel in; power out) — −1 fuel → +10 energy.
- **Solar** (power out) — +1 energy/тик (бесплатно, медленно).
- **Sensor** (signal out) — шлёт signal по порогу измеренного (напр. уровень Container).
- **Switch** (signal in; gate power/fluid) — пропускает поток при signal=on (логика/автоматизация).
- **Fabricator** (power+item in; item out) — по чертежу: ресурсы → компонент.
- **Hatch** (item bi) — точка взаимодействия агента (grab/deposit).

## E. Источники мира (даны, не крафтятся)
`OreDeposit` (mine → ore; конечен / медленный реген), `FuelVein`, `WaterSource`, `SunTile`.
Конечность ресурсов — двигатель экономики.

## F. Базовые глаголы агента (intents)
`sense(target)`, `move(dir)`, `grab/deposit(obj, R, n)`, `attach(portA, portB)`, `detach`,
`transfer(R, from, to, n)`, `transform(obj)`, `signal(target, msg)`, `build(component, at)`.
Всё остальное — комбинации. Каждый intent стоит energy + занимает тик.

## G. Стартовые рецепты-сборки (известные полезные комбо)
Рецепт = расстановка компонентов; движок сам считает, что она делает. Семена:
- **Майнер:** Frame + Drill + Generator + Container(ore) на OreDeposit → авто-добыча ore (жжёт fuel).
- **Плавильня:** Frame + Furnace + Container(fuel) + Container(ore) + power → metal.
- **Генератор-станция:** Generator + Container(fuel) → energy в Wire/Battery.
- **Соляр-ферма:** Solar×N + Wire + Battery(Container energy) → пассивная energy.
- **Водокачка:** Pump + Pipe + WaterSource + Container(water).
- **Автоматизация:** Sensor(уровень бака) + Switch + Pump → качает, пока бак не полон.
- **Фабрикатор-цех:** Fabricator + чертёж → штампует компоненты.

## H. Числовые конверсии (целочисленно — сохранение)
- mine: `5 energy → 1 ore` (Drill на OreDeposit)
- generate: `1 fuel → 10 energy` (Generator)
- solar: `тик → 1 energy`
- smelt: `2 ore + 1 fuel + 5 energy → 1 metal` (Furnace)
- fabricate: `3 metal → 1 Pipe|Wire`; `4 metal → 1 Container`; `5 metal + 1 crystal → 1 Pump`;
  `8 metal + 2 crystal → 1 Drill`; `10 metal + 3 crystal → 1 Fabricator`
- (коэффициенты — болванка под баланс)

## I. Цикл агента (bootstrap)
`sense → решает → intents (бюджет energy/тик)`. Путь старта: пришёл к OreDeposit → поставил
Drill+Generator → добыл ore → собрал Furnace → metal → Fabricator → свои компоненты → большие машины →
излишки на продажу другим агентам (через Hatch + signal).
**Стартовый споун агента:** немного `metal` + 1 базовый `Fabricator` (минимальный «принтер»), чтобы было с чего начать.

## J. Почему это лёгкое
- Нет непрерывной физики/коллизий/3D — только сетка + целые числа + тики.
- behaviors — крошечные чистые функции; тик = прогнать список удовлетворённых компонентов.
- Состояние сериализуемо (SQLite/JSON), детерминизм → реплеи/аудит дёшевы.
- Один процесс тянет тысячи сущностей; масштаб — шардинг по регионам сетки.

## K. Что решить дальше
- Баланс коэффициентов и стоимости intents.
- Глубина `sense` (что видно агенту/зрителю) и формат observation (JSON).
- Реген/конечность источников (живая экономика vs застой).
- Протокол подключения агента (REST/MCP, тик-синхронизация: pull-observe → push-intents).
- Чертежи (blueprint): формат + передача/продажа между агентами.
