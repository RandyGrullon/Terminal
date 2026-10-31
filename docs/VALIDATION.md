# Validación walk-forward de NEXUS + filtro «¿Tomo esta operación?»

Pregunta: **¿tiene NEXUS + filtro una ventaja real**, es decir, gana más de lo que ganaría cualquiera entrando al
azar con las mismas reglas de riesgo y los mismos costes?

Ejecución del 31/10/2026 (12:59 UTC) con datos públicos de Binance: BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT y BNBUSDT en
15m, 1h, 4h y 1d (20 series, hasta 4.999 velas cerradas cada una). Informe completo reducido:
[`docs/validation/binance-2026-10-31T12-59Z.json`](validation/binance-2026-10-31T12-59Z.json).

Versión medida: el motor NEXUS y el filtro **finales de la ronda de mejoras de octubre de 2026** (el código del
commit `8191643` más los cambios de esa ronda, entre ellos la corrección del repintado del motor), con los ajustes por
defecto. Al medir, esos cambios aún no tenían commit propio; la huella de los 21 archivos de `backend/app/engine/`
que usa la validación, `0afc602bbbbf`, permite comprobar si un commit contiene exactamente ese motor (cómo
recalcularla en la sección 6). Si el motor o sus ajustes cambian, la validación hay que repetirla: estos números no se
trasladan a otra versión. La ejecución anterior, con el motor del commit `8191643`, está resumida en la sección 7.

> **Conclusión: sin evidencia de ventaja.** Con los parámetros por defecto, NEXUS + filtro no gana más que entradas
> al azar con las mismas reglas de riesgo. En conjunto pierde **−0,08R por operación** (437 operaciones; intervalo del
> 95 % por bloques de tiempo −0,18R a +0,02R) y queda **0,13R por debajo del azar** del mismo periodo y lado (IC 95 %
> −0,22R a −0,03R). Sin el filtro, NEXUS **pierde claramente después de costes**: −0,23R por operación en 1.907
> operaciones (IC 95 % −0,31R a −0,13R), también por debajo del azar. El filtro reduce la pérdida sobre todo porque
> opera mucho menos, no porque elija mejor. Qué implica para usar la app: sección 4. Nada de esto justifica operar
> con dinero real.

Resultados simulados con datos históricos: no garantizan resultados futuros. No es asesoría financiera.

---------------------------------------------------------------------------------------------------------------------
## 1. Método

### 1.1 Sin mirar el futuro

- NEXUS se ejecuta **una sola vez, de forma causal, sobre todo el historial** de cada serie (`run_nexus`): aprende en
  línea y la decisión de cada vela solo usa las velas cerradas hasta ese momento. Cada etiqueta se aprende cuando
  ya se conoce (h velas después). Por eso **todas las decisiones son fuera de muestra**.
- El filtro parte de **sus pesos iniciales**: no usa lo que aprendió en vivo de resultados reales (eso sería mirar el
  futuro de las decisiones antiguas).
- Las primeras **500 velas** son calentamiento: el modelo aprende de ellas, pero sus decisiones no se puntúan.
- El resto se reparte en **5 periodos consecutivos** (walk-forward). Cada operación cuenta en el periodo de su vela
  de decisión. Solo se usan velas cerradas; la vela en formación se descarta.
- Ningún parámetro se ajustó después de ver los resultados: son los valores por defecto de la app
  (umbral de entrada 0,20, stop 2 ATR o más allá del último giro, objetivos 1R/2R/3R, cierre por tiempo a las 48
  velas sin objetivo 1, filtro TOMAR desde 65/100).

### 1.2 Lo que se compara (mismas reglas de riesgo y costes en todo)

| Variante | Qué es |
|---|---|
| **NEXUS + filtro** | El producto: las operaciones que NEXUS abre cuando el filtro dice TOMAR, con sus propias salidas (stop, objetivos con cierres parciales, stop en equilibrio y dinámico, cierre por tiempo y las salidas del modelo: anticipada, señal en contra, giro). |
| **NEXUS sin filtro** | Las mismas señales tomando todas las candidatas. |
| **Mismas salidas** | Las entradas de NEXUS (con y sin filtro) gestionadas solo con las salidas mecánicas: stop, objetivos 1-3, stop en equilibrio, stop dinámico y cierre por tiempo. Es exactamente la clase `Position` del motor (una prueba confirma que reproduce el R y la vela de salida de cada operación de NEXUS que terminó por una regla mecánica). |
| **Azar** | Monte Carlo de 1.000 secuencias: en cada periodo, el mismo número de operaciones y la misma mezcla largo/corto que la variante comparada, en velas elegidas al azar, con los niveles por defecto de NEXUS en esa vela y las salidas mecánicas. Es la referencia de «ninguna habilidad». |
| **Comprar y mantener** | Comprar al principio del periodo de prueba y vender al final (retorno, caída máxima y Sharpe). Arriesga todo el capital: no es comparable en riesgo. |

Costes: comisión de la clase de activo (`app.engine.costs`: **0,1 % por lado** para cripto) incluida en cada R, más
un **deslizamiento de 0,02 %** por ejecución. Cuenta simulada: 1 % del capital arriesgado por operación,
apalancamiento máximo 5× (la regla de los backtests de la app).

### 1.3 Métricas

- **Expectativa** = R medio por operación después de comisiones y deslizamiento (1R = la distancia al stop), con
  su **intervalo del 95 % por bootstrap** (2.000 remuestreos). Con **menos de 10 operaciones no se calcula
  intervalo**: remuestrear un puñado de operaciones da intervalos mucho más estrechos que la incertidumbre real. Entre
  10 y 29 operaciones el intervalo se marca como **«muy incierto»**, y por debajo de 30 nunca se saca una conclusión.
  Además: acierto («x de n»), factor de beneficio, caída máxima y Sharpe anualizado de la cuenta simulada.
- **Caídas máximas**: la cuenta simulada y comprar y mantener se valoran vela a vela. Para comparar con el azar se usa
  otra medida, la **caída de la curva de operaciones cerradas** en orden temporal con el mismo riesgo por operación,
  que se calcula igual para NEXUS y para cada secuencia al azar (tabla 2.5).
- **Percentil frente al azar**: dónde cae la expectativa de NEXUS dentro de las 1.000 secuencias al azar
  (50 = como el azar; hace falta 95 o más para hablar de ventaja).
- **En conjunto** (por temporalidad y todas juntas) las cinco criptomonedas **no son pruebas independientes**: su
  correlación diaria media es 0,63 (0,84 en la ventana de 15m), así que equivalen a unos 1,1-1,4 activos
  independientes. Por eso los intervalos de conjunto **remuestrean bloques de tiempo** (de 2 a 30 días según la
  temporalidad): todas las operaciones de todos los activos del mismo bloque entran o salen juntas. La comparación
  con el azar en conjunto usa el **exceso** de cada operación sobre el R medio de una entrada al azar del mismo
  periodo y lado de su propia serie, con el mismo intervalo por bloques.

### 1.4 Regla del veredicto

«**Indicios de ventaja**» solo si se cumple todo: al menos 30 operaciones; intervalo del 95 % de la expectativa por
encima de 0; percentil frente al azar de 95 o más (en conjunto: exceso sobre el azar con intervalo por encima de 0);
y resultado positivo en la mayoría de los periodos, con operaciones en al menos dos (o en la mayoría de las series). En
cualquier otro caso: «**Sin evidencia de ventaja**», con el motivo concreto («muestra insuficiente», «pierde después
de comisiones», «el intervalo incluye el 0», «no supera claramente al azar»…). Con 20 series y varias variantes, alguna
podría salir favorable por pura casualidad (comparaciones múltiples): un resultado aislado no bastaría.

---------------------------------------------------------------------------------------------------------------------
## 2. Resultados

### 2.1 Resumen

- **El producto opera poco**: 437 operaciones cerradas en las 20 series (303 largos, 134 cortos); el filtro rechazó
  2.080 señales en todo el historial (calentamiento incluido). Solo 5 de las 20 series llegan a 30 operaciones (BTC 4h,
  BTC 1d, ETH 1d, SOL 4h y BNB 1d) y ninguna muestra ventaja: 15 series quedan en «muestra insuficiente», 4 tienen
  muestra suficiente pero ninguna ventaja y una pierde con el intervalo entero por debajo de 0 (SOLUSDT 4h: −0,27R por
  operación en 43 operaciones; IC 95 % −0,50R a −0,04R). En 6 series hay menos de 10 operaciones y por tanto ningún
  intervalo; en otras 9, entre 10 y 29 («muy incierto»). El mejor percentil frente al azar es 82 (BTCUSDT 1h, con solo
  5 operaciones); el peor, 4 (SOLUSDT 1d).
- **En conjunto, el producto pierde −0,08R por operación** (IC 95 % por bloques −0,18R a +0,02R; acierto 44 %) y está
  **0,13R por debajo del azar** del mismo periodo y lado (IC 95 % −0,22R a −0,03R). Solo 6 de 20 series tienen
  expectativa positiva. En 4h pierde incluso con el intervalo entero por debajo de 0 (−0,17R en 156 operaciones; IC
  por bloques −0,32R a −0,02R).
- **Sin el filtro, NEXUS pierde con claridad**: −0,23R por operación en 1.907 operaciones (IC 95 % por bloques −0,31R
  a −0,13R); 11 de las 20 series tienen el intervalo entero por debajo de 0. Frente a su propio azar queda 0,10R por
  debajo (IC −0,17R a −0,06R); su mejor percentil es 70.
- **Entradas frente a salidas**: con las mismas salidas mecánicas que el azar, las entradas de NEXUS sin filtro rinden
  0,12R menos que entradas al azar (IC −0,18R a −0,07R); sus salidas propias apenas cambian nada en promedio (+0,02R;
  IC −0,02R a +0,04R). Es decir, **la señal de entrada no aporta ventaja** en estos datos. Con filtro, sus entradas
  también quedan 0,12R por debajo del azar con las mismas salidas (IC −0,24R a 0,00R) y sus salidas suman 0,00R (IC
  −0,06R a +0,04R). El único efecto favorable con intervalo por encima de 0 es pequeño y aislado: sin filtro en 15m,
  las salidas del modelo suman +0,06R por operación (IC +0,01R a +0,11R) sobre unas operaciones que pierden −0,42R; con
  tantas comparaciones, uno así es esperable por casualidad.
- **Costes**: en 15m incluso las entradas al azar pierden (mediana de −0,25R a −0,58R por operación según el par, con
  el número de operaciones de NEXUS sin filtro) porque la comisión de 0,1 % por lado es grande frente a un stop de
  pocas décimas de %. El filtro casi no opera ahí (26 operaciones en cinco pares, −0,14R; IC −0,52R a +0,06R, muy
  incierto).
- **El filtro** mejora la pérdida media del conjunto (−0,08R frente a −0,23R), sobre todo porque opera mucho menos (437
  frente a 1.907 operaciones), en especial en 15m y 1h. Serie a serie su efecto nunca es significativo (15 «muestra
  insuficiente», 5 «sin evidencia de efecto»), y sus operaciones siguen sin superar al azar.
- **Comprar y mantener** ganó mucho en 1d en cuatro de los cinco pares (de +383 % a +4.556 %; SOL, medido desde
  finales de 2021, perdió un 49 %), siempre con caídas del 76-95 %; en 15m fue negativo en los cinco pares y en 1h
  mixto (de −13,2 % a +10,5 %). La cuenta de NEXUS (1 % de riesgo por operación) se movió entre −11,2 % y +8,4 % con
  caídas de hasta el 12,5 %: lo bajo de esas caídas viene del poco riesgo y de lo poco que opera, no de una habilidad.
  Con la medida comparable (caída de las operaciones cerradas, tabla 2.5), NEXUS cae más que la mediana del azar en 7
  de las 20 series, menos en 12 y lo mismo en una: tampoco ahí hay una diferencia sistemática. La caída de la cuenta,
  valorada vela a vela, es entre 0,03 y 1,15 puntos mayor que la de sus operaciones cerradas.

### 2.2 Datos

| Serie | Velas | Desde | Hasta | Prueba desde | Huecos | Comisión/lado |
| --- | --- | --- | --- | --- | --- | --- |
| BINANCE:BTCUSDT 15m | 4999 | 2026-09-09 | 2026-10-31 | 2026-09-14 | 0 | 0,100 % |
| BINANCE:BTCUSDT 1h | 4999 | 2026-04-06 | 2026-10-31 | 2026-04-27 | 0 | 0,100 % |
| BINANCE:BTCUSDT 4h | 4999 | 2024-07-20 | 2026-10-31 | 2024-10-11 | 0 | 0,100 % |
| BINANCE:BTCUSDT 1d | 3362 | 2017-08-17 | 2026-10-30 | 2018-12-30 | 0 | 0,100 % |
| BINANCE:ETHUSDT 15m | 4999 | 2026-09-09 | 2026-10-31 | 2026-09-14 | 0 | 0,100 % |
| BINANCE:ETHUSDT 1h | 4999 | 2026-04-06 | 2026-10-31 | 2026-04-27 | 0 | 0,100 % |
| BINANCE:ETHUSDT 4h | 4999 | 2024-07-20 | 2026-10-31 | 2024-10-11 | 0 | 0,100 % |
| BINANCE:ETHUSDT 1d | 3362 | 2017-08-17 | 2026-10-30 | 2018-12-30 | 0 | 0,100 % |
| BINANCE:SOLUSDT 15m | 4999 | 2026-09-09 | 2026-10-31 | 2026-09-14 | 0 | 0,100 % |
| BINANCE:SOLUSDT 1h | 4999 | 2026-04-06 | 2026-10-31 | 2026-04-27 | 0 | 0,100 % |
| BINANCE:SOLUSDT 4h | 4999 | 2024-07-20 | 2026-10-31 | 2024-10-11 | 0 | 0,100 % |
| BINANCE:SOLUSDT 1d | 2272 | 2020-08-11 | 2026-10-30 | 2021-12-24 | 0 | 0,100 % |
| BINANCE:XRPUSDT 15m | 4999 | 2026-09-09 | 2026-10-31 | 2026-09-14 | 0 | 0,100 % |
| BINANCE:XRPUSDT 1h | 4999 | 2026-04-06 | 2026-10-31 | 2026-04-27 | 0 | 0,100 % |
| BINANCE:XRPUSDT 4h | 4999 | 2024-07-20 | 2026-10-31 | 2024-10-11 | 0 | 0,100 % |
| BINANCE:XRPUSDT 1d | 3102 | 2018-05-04 | 2026-10-30 | 2019-09-16 | 0 | 0,100 % |
| BINANCE:BNBUSDT 15m | 4999 | 2026-09-09 | 2026-10-31 | 2026-09-14 | 0 | 0,100 % |
| BINANCE:BNBUSDT 1h | 4999 | 2026-04-06 | 2026-10-31 | 2026-04-27 | 0 | 0,100 % |
| BINANCE:BNBUSDT 4h | 4999 | 2024-07-20 | 2026-10-31 | 2024-10-11 | 0 | 0,100 % |
| BINANCE:BNBUSDT 1d | 3281 | 2017-11-06 | 2026-10-30 | 2019-03-21 | 0 | 0,100 % |

### 2.3 NEXUS + filtro frente a las referencias (R por operación, después de costes)

| Serie | Ganadas | Expectativa | IC 95 % | PF | Sin filtro (ops) | Mismas salidas | Azar p5 / p50 / p95 | Percentil | Percentil mismas salidas | Veredicto |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BINANCE:BTCUSDT 15m | 0 de 1 | −0,59R | sin intervalo: menos de 10 operaciones | 0,00 | −0,56R (112) | −0,59R | −1,58R / −0,07R / +1,66R | 36 | 36 | Sin evidencia de ventaja |
| BINANCE:BTCUSDT 1h | 3 de 5 | +0,27R | sin intervalo: menos de 10 operaciones | 2,17 | −0,23R (116) | +0,03R | −1,00R / −0,20R / +0,64R | 82 | 68 | Sin evidencia de ventaja |
| BINANCE:BTCUSDT 4h | 29 de 63 | −0,11R | −0,35R a +0,13R | 0,76 | −0,15R (90) | −0,11R | −0,27R / −0,06R / +0,16R | 34 | 35 | Sin evidencia de ventaja |
| BINANCE:BTCUSDT 1d | 20 de 45 | +0,04R | −0,26R a +0,36R | 1,10 | +0,01R (63) | +0,06R | −0,06R / +0,23R / +0,53R | 14 | 16 | Sin evidencia de ventaja |
| BINANCE:ETHUSDT 15m | 1 de 1 | +0,19R | sin intervalo: menos de 10 operaciones | — | −0,37R (83) | +0,19R | −1,40R / +0,03R / +1,72R | 66 | 65 | Sin evidencia de ventaja |
| BINANCE:ETHUSDT 1h | 6 de 16 | −0,39R | −0,80R a +0,06R · muy incierto | 0,40 | −0,36R (94) | −0,47R | −0,57R / −0,13R / +0,34R | 18 | 12 | Sin evidencia de ventaja |
| BINANCE:ETHUSDT 4h | 5 de 21 | −0,29R | −0,67R a +0,15R · muy incierto | 0,49 | −0,16R (111) | −0,14R | −0,41R / −0,05R / +0,36R | 14 | 36 | Sin evidencia de ventaja |
| BINANCE:ETHUSDT 1d | 36 de 67 | +0,12R | −0,12R a +0,36R | 1,33 | +0,07R (85) | +0,09R | −0,10R / +0,14R / +0,36R | 46 | 37 | Sin evidencia de ventaja |
| BINANCE:SOLUSDT 15m | 9 de 15 | −0,12R | −0,52R a +0,24R · muy incierto | 0,68 | −0,24R (106) | +0,08R | −0,52R / −0,07R / +0,43R | 44 | 69 | Sin evidencia de ventaja |
| BINANCE:SOLUSDT 1h | 3 de 12 | −0,10R | −0,60R a +0,50R · muy incierto | 0,78 | −0,16R (106) | +0,21R | −0,47R / +0,11R / +0,70R | 28 | 59 | Sin evidencia de ventaja |
| BINANCE:SOLUSDT 4h | 14 de 43 | −0,27R | −0,50R a −0,04R | 0,45 | −0,11R (122) | −0,32R | −0,33R / −0,06R / +0,24R | 10 | 6 | Sin evidencia de ventaja |
| BINANCE:SOLUSDT 1d | 12 de 29 | −0,22R | −0,52R a +0,11R · muy incierto | 0,56 | −0,18R (42) | −0,48R | −0,19R / +0,13R / +0,45R | 4 | 0 | Sin evidencia de ventaja |
| BINANCE:XRPUSDT 15m | 2 de 8 | −0,21R | sin intervalo: menos de 10 operaciones | 0,61 | −0,40R (87) | −0,35R | −0,71R / −0,08R / +0,59R | 39 | 27 | Sin evidencia de ventaja |
| BINANCE:XRPUSDT 1h | 6 de 12 | −0,02R | −0,46R a +0,47R · muy incierto | 0,94 | −0,19R (111) | −0,05R | −0,61R / −0,06R / +0,45R | 56 | 51 | Sin evidencia de ventaja |
| BINANCE:XRPUSDT 4h | 12 de 27 | +0,03R | −0,38R a +0,44R · muy incierto | 1,07 | −0,15R (134) | +0,18R | −0,31R / +0,02R / +0,37R | 53 | 78 | Sin evidencia de ventaja |
| BINANCE:XRPUSDT 1d | 5 de 10 | −0,13R | −0,56R a +0,33R · muy incierto | 0,64 | −0,27R (50) | −0,34R | −0,63R / −0,13R / +0,45R | 50 | 28 | Sin evidencia de ventaja |
| BINANCE:BNBUSDT 15m | 1 de 1 | +0,14R | sin intervalo: menos de 10 operaciones | — | −0,51R (112) | +0,14R | −1,96R / −1,18R / +1,61R | 72 | 72 | Sin evidencia de ventaja |
| BINANCE:BNBUSDT 1h | 7 de 12 | −0,09R | −0,58R a +0,40R · muy incierto | 0,78 | −0,24R (99) | −0,08R | −0,67R / −0,23R / +0,29R | 65 | 66 | Sin evidencia de ventaja |
| BINANCE:BNBUSDT 4h | 0 de 2 | −1,05R | sin intervalo: menos de 10 operaciones | 0,00 | −0,22R (109) | −1,05R | −1,07R / +0,10R / +1,64R | 13 | 13 | Sin evidencia de ventaja |
| BINANCE:BNBUSDT 1d | 22 de 47 | −0,04R | −0,34R a +0,26R | 0,92 | −0,12R (75) | +0,06R | −0,07R / +0,20R / +0,48R | 8 | 19 | Sin evidencia de ventaja |

IC 95 %: sin intervalo con menos de 10 operaciones; «muy incierto» con menos de 30 (sin conclusiones por debajo de 30).

### 2.4 NEXUS sin filtro frente a su propio azar (mismo número de operaciones y mezcla largo/corto)

| Serie | Ganadas | Expectativa | IC 95 % | PF | Azar p5 / p50 / p95 | Percentil | Periodos positivos | Veredicto |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BINANCE:BTCUSDT 15m | 29 de 112 | −0,56R | −0,74R a −0,37R | 0,26 | −0,75R / −0,58R / −0,42R | 57 | 0 de 5 | Sin evidencia de ventaja |
| BINANCE:BTCUSDT 1h | 52 de 116 | −0,23R | −0,40R a −0,06R | 0,57 | −0,39R / −0,22R / −0,05R | 47 | 0 de 5 | Sin evidencia de ventaja |
| BINANCE:BTCUSDT 4h | 43 de 90 | −0,15R | −0,34R a +0,04R | 0,68 | −0,23R / −0,06R / +0,14R | 20 | 1 de 5 | Sin evidencia de ventaja |
| BINANCE:BTCUSDT 1d | 29 de 63 | +0,01R | −0,25R a +0,28R | 1,03 | −0,04R / +0,19R / +0,43R | 11 | 3 de 5 | Sin evidencia de ventaja |
| BINANCE:ETHUSDT 15m | 29 de 83 | −0,37R | −0,56R a −0,17R | 0,35 | −0,56R / −0,37R / −0,17R | 49 | 1 de 5 | Sin evidencia de ventaja |
| BINANCE:ETHUSDT 1h | 35 de 94 | −0,36R | −0,55R a −0,16R | 0,42 | −0,37R / −0,19R / −0,02R | 6 | 0 de 5 | Sin evidencia de ventaja |
| BINANCE:ETHUSDT 4h | 44 de 111 | −0,16R | −0,33R a +0,01R | 0,67 | −0,24R / −0,08R / +0,09R | 21 | 0 de 5 | Sin evidencia de ventaja |
| BINANCE:ETHUSDT 1d | 45 de 85 | +0,07R | −0,12R a +0,26R | 1,20 | −0,04R / +0,16R / +0,37R | 22 | 2 de 5 | Sin evidencia de ventaja |
| BINANCE:SOLUSDT 15m | 42 de 106 | −0,24R | −0,41R a −0,06R | 0,55 | −0,47R / −0,30R / −0,12R | 70 | 1 de 5 | Sin evidencia de ventaja |
| BINANCE:SOLUSDT 1h | 41 de 106 | −0,16R | −0,33R a +0,01R | 0,65 | −0,30R / −0,12R / +0,06R | 33 | 1 de 5 | Sin evidencia de ventaja |
| BINANCE:SOLUSDT 4h | 50 de 122 | −0,11R | −0,26R a +0,03R | 0,73 | −0,18R / −0,03R / +0,15R | 18 | 2 de 5 | Sin evidencia de ventaja |
| BINANCE:SOLUSDT 1d | 16 de 42 | −0,18R | −0,47R a +0,12R | 0,65 | −0,16R / +0,11R / +0,43R | 4 | 2 de 5 | Sin evidencia de ventaja |
| BINANCE:XRPUSDT 15m | 28 de 87 | −0,40R | −0,57R a −0,22R | 0,32 | −0,44R / −0,25R / −0,07R | 9 | 0 de 5 | Sin evidencia de ventaja |
| BINANCE:XRPUSDT 1h | 44 de 111 | −0,19R | −0,37R a −0,01R | 0,63 | −0,22R / −0,07R / +0,12R | 9 | 0 de 5 | Sin evidencia de ventaja |
| BINANCE:XRPUSDT 4h | 59 de 134 | −0,15R | −0,31R a +0,01R | 0,69 | −0,16R / −0,01R / +0,16R | 6 | 0 de 5 | Sin evidencia de ventaja |
| BINANCE:XRPUSDT 1d | 20 de 50 | −0,27R | −0,49R a −0,04R | 0,46 | −0,31R / −0,07R / +0,19R | 8 | 0 de 5 | Sin evidencia de ventaja |
| BINANCE:BNBUSDT 15m | 37 de 112 | −0,51R | −0,70R a −0,31R | 0,34 | −0,63R / −0,45R / −0,28R | 31 | 0 de 5 | Sin evidencia de ventaja |
| BINANCE:BNBUSDT 1h | 44 de 99 | −0,24R | −0,43R a −0,05R | 0,55 | −0,36R / −0,17R / +0,01R | 29 | 1 de 5 | Sin evidencia de ventaja |
| BINANCE:BNBUSDT 4h | 42 de 109 | −0,22R | −0,39R a −0,03R | 0,58 | −0,24R / −0,08R / +0,09R | 9 | 0 de 5 | Sin evidencia de ventaja |
| BINANCE:BNBUSDT 1d | 37 de 75 | −0,12R | −0,33R a +0,10R | 0,75 | −0,03R / +0,18R / +0,42R | 0 | 2 de 5 | Sin evidencia de ventaja |

### 2.5 Cuenta simulada (1 % de riesgo por operación) y comprar y mantener

| Serie | Retorno de la cuenta | DD de la cuenta (vela a vela) | Sharpe de la cuenta | DD por operaciones NEXUS | DD por operaciones azar (mediana) | Comprar y mantener | DD comprar y mantener | Sharpe comprar y mantener |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BINANCE:BTCUSDT 15m | −0,6 % | 1,2 % | −2,00 | 0,6 % | 0,1 % | −1,3 % | 12,3 % | −0,13 |
| BINANCE:BTCUSDT 1h | +1,3 % | 1,3 % | 0,81 | 0,8 % | 2,4 % | −2,1 % | 29,4 % | 0,08 |
| BINANCE:BTCUSDT 4h | −7,2 % | 9,3 % | −0,62 | 8,7 % | 10,4 % | +24,7 % | 53,4 % | 0,46 |
| BINANCE:BTCUSDT 1d | +1,8 % | 5,9 % | 0,10 | 5,0 % | 6,1 % | +1.993,7 % | 76,6 % | 0,95 |
| BINANCE:ETHUSDT 15m | +0,2 % | 0,8 % | 0,54 | 0,0 % | 0,0 % | −10,2 % | 21,7 % | −1,61 |
| BINANCE:ETHUSDT 1h | −6,1 % | 7,6 % | −2,05 | 7,3 % | 5,1 % | −5,9 % | 36,9 % | 0,01 |
| BINANCE:ETHUSDT 4h | −6,1 % | 8,0 % | −1,08 | 7,9 % | 5,1 % | −8,0 % | 68,0 % | 0,27 |
| BINANCE:ETHUSDT 1d | +8,4 % | 6,4 % | 0,37 | 5,5 % | 7,3 % | +1.589,8 % | 79,3 % | 0,86 |
| BINANCE:SOLUSDT 15m | −1,9 % | 4,3 % | −1,43 | 3,2 % | 4,5 % | −5,8 % | 25,3 % | −0,56 |
| BINANCE:SOLUSDT 1h | −1,2 % | 3,5 % | −0,60 | 3,0 % | 3,4 % | +9,8 % | 38,0 % | 0,60 |
| BINANCE:SOLUSDT 4h | −11,2 % | 12,5 % | −1,39 | 12,3 % | 8,2 % | −33,6 % | 78,5 % | 0,10 |
| BINANCE:SOLUSDT 1d | −6,2 % | 8,6 % | −0,61 | 8,0 % | 4,7 % | −49,3 % | 95,1 % | 0,31 |
| BINANCE:XRPUSDT 15m | −1,7 % | 4,6 % | −1,86 | 4,1 % | 2,7 % | −10,9 % | 26,7 % | −1,13 |
| BINANCE:XRPUSDT 1h | −0,3 % | 2,2 % | −0,10 | 1,6 % | 3,6 % | −13,2 % | 35,5 % | −0,15 |
| BINANCE:XRPUSDT 4h | +0,7 % | 5,0 % | 0,12 | 4,9 % | 6,0 % | +132,2 % | 72,5 % | 0,91 |
| BINANCE:XRPUSDT 1d | −1,3 % | 4,2 % | −0,18 | 3,3 % | 3,5 % | +382,9 % | 83,2 % | 0,70 |
| BINANCE:BNBUSDT 15m | +0,1 % | 0,7 % | 0,44 | 0,0 % | 1,2 % | −2,2 % | 15,2 % | −0,23 |
| BINANCE:BNBUSDT 1h | −1,2 % | 5,0 % | −0,44 | 4,3 % | 4,4 % | +10,5 % | 26,7 % | 0,68 |
| BINANCE:BNBUSDT 4h | −2,1 % | 2,1 % | −1,09 | 2,1 % | 1,1 % | +23,3 % | 59,6 % | 0,46 |
| BINANCE:BNBUSDT 1d | −2,1 % | 6,2 % | −0,10 | 5,3 % | 5,9 % | +4.556,2 % | 76,1 % | 1,02 |

«DD por operaciones» es la caída máxima de la curva de operaciones cerradas en orden temporal, con el mismo riesgo por operación: la misma medida para NEXUS y para la mediana de las secuencias al azar. La cuenta se valora vela a vela (pérdidas latentes de la operación abierta, límite de apalancamiento), así que su caída no es comparable con la del azar; sí con la de comprar y mantener, también vela a vela.

### 2.6 Periodos walk-forward (expectativa de NEXUS + filtro · operaciones)

| Serie | P1 | P2 | P3 | P4 | P5 | Periodos positivos |
| --- | --- | --- | --- | --- | --- | --- |
| BINANCE:BTCUSDT 15m | −0,59R · 1 | — · 0 | — · 0 | — · 0 | — · 0 | 0 de 1 |
| BINANCE:BTCUSDT 1h | +0,42R · 3 | +0,05R · 2 | — · 0 | — · 0 | — · 0 | 2 de 2 |
| BINANCE:BTCUSDT 4h | −0,21R · 13 | −0,37R · 9 | +0,23R · 11 | −0,13R · 18 | −0,11R · 12 | 1 de 5 |
| BINANCE:BTCUSDT 1d | +0,12R · 8 | +0,12R · 15 | −0,42R · 4 | +0,05R · 11 | +0,06R · 7 | 4 de 5 |
| BINANCE:ETHUSDT 15m | +0,19R · 1 | — · 0 | — · 0 | — · 0 | — · 0 | 1 de 1 |
| BINANCE:ETHUSDT 1h | — · 0 | −0,11R · 8 | +0,26R · 1 | −0,66R · 5 | −1,13R · 2 | 1 de 4 |
| BINANCE:ETHUSDT 4h | −0,36R · 9 | −0,39R · 7 | +0,44R · 2 | — · 0 | −0,36R · 3 | 1 de 4 |
| BINANCE:ETHUSDT 1d | −0,51R · 7 | +0,27R · 26 | +0,08R · 9 | +0,02R · 14 | +0,32R · 11 | 4 de 5 |
| BINANCE:SOLUSDT 15m | +0,08R · 8 | −0,35R · 7 | — · 0 | — · 0 | — · 0 | 1 de 2 |
| BINANCE:SOLUSDT 1h | — · 0 | −0,61R · 2 | — · 0 | −0,16R · 7 | +0,41R · 3 | 1 de 3 |
| BINANCE:SOLUSDT 4h | +0,17R · 5 | −0,19R · 21 | −0,45R · 11 | −0,72R · 3 | −0,51R · 3 | 1 de 5 |
| BINANCE:SOLUSDT 1d | −0,10R · 11 | −0,65R · 6 | +0,08R · 8 | −0,47R · 4 | — · 0 | 1 de 4 |
| BINANCE:XRPUSDT 15m | +0,43R · 4 | −0,39R · 1 | — · 0 | — · 0 | −0,99R · 3 | 1 de 3 |
| BINANCE:XRPUSDT 1h | −0,50R · 2 | +0,06R · 2 | — · 0 | +0,18R · 5 | −0,10R · 3 | 2 de 4 |
| BINANCE:XRPUSDT 4h | +0,16R · 13 | +0,39R · 7 | −0,83R · 3 | −0,12R · 3 | −1,06R · 1 | 2 de 5 |
| BINANCE:XRPUSDT 1d | +0,21R · 4 | −0,84R · 3 | — · 0 | — · 0 | +0,14R · 3 | 2 de 3 |
| BINANCE:BNBUSDT 15m | — · 0 | — · 0 | +0,14R · 1 | — · 0 | — · 0 | 1 de 1 |
| BINANCE:BNBUSDT 1h | +1,90R · 1 | −0,25R · 10 | — · 0 | −0,53R · 1 | — · 0 | 1 de 3 |
| BINANCE:BNBUSDT 4h | — · 0 | — · 0 | −1,05R · 2 | — · 0 | — · 0 | 0 de 1 |
| BINANCE:BNBUSDT 1d | −0,20R · 14 | +0,19R · 16 | −0,45R · 4 | +0,28R · 6 | −0,28R · 7 | 2 de 5 |

### 2.7 Efecto del filtro «¿Tomo esta operación?»

| Serie | Con filtro | Sin filtro | Diferencia | IC 95 % | Lectura |
| --- | --- | --- | --- | --- | --- |
| BINANCE:BTCUSDT 15m | −0,59R (1) | −0,56R (112) | −0,03R | sin intervalo: menos de 10 operaciones | muestra insuficiente |
| BINANCE:BTCUSDT 1h | +0,27R (5) | −0,23R (116) | +0,50R | sin intervalo: menos de 10 operaciones | muestra insuficiente |
| BINANCE:BTCUSDT 4h | −0,11R (63) | −0,15R (90) | +0,04R | −0,26R a +0,36R | sin evidencia de efecto |
| BINANCE:BTCUSDT 1d | +0,04R (45) | +0,01R (63) | +0,03R | −0,38R a +0,45R | sin evidencia de efecto |
| BINANCE:ETHUSDT 15m | +0,19R (1) | −0,37R (83) | +0,57R | sin intervalo: menos de 10 operaciones | muestra insuficiente |
| BINANCE:ETHUSDT 1h | −0,39R (16) | −0,36R (94) | −0,03R | −0,53R a +0,45R · muy incierto | muestra insuficiente |
| BINANCE:ETHUSDT 4h | −0,29R (21) | −0,16R (111) | −0,13R | −0,55R a +0,34R · muy incierto | muestra insuficiente |
| BINANCE:ETHUSDT 1d | +0,12R (67) | +0,07R (85) | +0,05R | −0,26R a +0,36R | sin evidencia de efecto |
| BINANCE:SOLUSDT 15m | −0,12R (15) | −0,24R (106) | +0,12R | −0,28R a +0,54R · muy incierto | muestra insuficiente |
| BINANCE:SOLUSDT 1h | −0,10R (12) | −0,16R (106) | +0,06R | −0,46R a +0,71R · muy incierto | muestra insuficiente |
| BINANCE:SOLUSDT 4h | −0,27R (43) | −0,11R (122) | −0,16R | −0,42R a +0,12R | sin evidencia de efecto |
| BINANCE:SOLUSDT 1d | −0,22R (29) | −0,18R (42) | −0,04R | −0,47R a +0,39R · muy incierto | muestra insuficiente |
| BINANCE:XRPUSDT 15m | −0,21R (8) | −0,40R (87) | +0,19R | sin intervalo: menos de 10 operaciones | muestra insuficiente |
| BINANCE:XRPUSDT 1h | −0,02R (12) | −0,19R (111) | +0,17R | −0,34R a +0,65R · muy incierto | muestra insuficiente |
| BINANCE:XRPUSDT 4h | +0,03R (27) | −0,15R (134) | +0,19R | −0,26R a +0,61R · muy incierto | muestra insuficiente |
| BINANCE:XRPUSDT 1d | −0,13R (10) | −0,27R (50) | +0,14R | −0,33R a +0,66R · muy incierto | muestra insuficiente |
| BINANCE:BNBUSDT 15m | +0,14R (1) | −0,51R (112) | +0,65R | sin intervalo: menos de 10 operaciones | muestra insuficiente |
| BINANCE:BNBUSDT 1h | −0,09R (12) | −0,24R (99) | +0,15R | −0,39R a +0,71R · muy incierto | muestra insuficiente |
| BINANCE:BNBUSDT 4h | −1,05R (2) | −0,22R (109) | −0,83R | sin intervalo: menos de 10 operaciones | muestra insuficiente |
| BINANCE:BNBUSDT 1d | −0,04R (47) | −0,12R (75) | +0,08R | −0,29R a +0,45R | sin evidencia de efecto |

### 2.8 Conjunto (intervalos por bloques de tiempo entre activos)

| Grupo | Series | Activos (≈ independientes) | ρ medio diario | Operaciones | Expectativa | IC 95 % por bloques | IC 95 % si fueran independientes | Exceso sobre azar (IC 95 %) | Series positivas | Veredicto |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Temporalidad 15m | 5 | 5 (≈ 1,1) | 0,84 | 26 | −0,14R | −0,52R a +0,06R · muy incierto | −0,44R a +0,16R · muy incierto | −0,05R (−0,37R a +0,11R · muy incierto) | 2 de 5 | Sin evidencia de ventaja |
| Temporalidad 1h | 5 | 5 (≈ 1,2) | 0,79 | 57 | −0,13R | −0,43R a +0,20R | −0,36R a +0,13R | −0,04R (−0,34R a +0,27R) | 1 de 5 | Sin evidencia de ventaja |
| Temporalidad 4h | 5 | 5 (≈ 1,3) | 0,72 | 156 | −0,17R | −0,32R a −0,02R | −0,32R a −0,01R | −0,13R (−0,27R a +0,01R) | 1 de 5 | Sin evidencia de ventaja |
| Temporalidad 1d | 5 | 5 (≈ 1,4) | 0,63 | 198 | 0,00R | −0,15R a +0,16R | −0,13R a +0,14R | −0,16R (−0,32R a 0,00R) | 2 de 5 | Sin evidencia de ventaja |
| Todas las series | 20 | 5 (≈ 1,4) | 0,63 | 437 | −0,08R | −0,18R a +0,02R | −0,18R a +0,01R | −0,13R (−0,22R a −0,03R) | 6 de 20 | Sin evidencia de ventaja |

### 2.9 Conjunto, NEXUS sin filtro

| Grupo | Operaciones | Expectativa | IC 95 % por bloques | IC 95 % si fueran independientes | Exceso sobre azar (IC 95 %) | Series positivas | Veredicto |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Temporalidad 15m | 500 | −0,42R | −0,54R a −0,31R | −0,50R a −0,34R | −0,02R (−0,11R a +0,07R) | 0 de 5 | Sin evidencia de ventaja |
| Temporalidad 1h | 526 | −0,23R | −0,36R a −0,12R | −0,31R a −0,15R | −0,08R (−0,20R a +0,03R) | 0 de 5 | Sin evidencia de ventaja |
| Temporalidad 4h | 566 | −0,16R | −0,24R a −0,07R | −0,24R a −0,09R | −0,11R (−0,21R a −0,02R) | 0 de 5 | Sin evidencia de ventaja |
| Temporalidad 1d | 315 | −0,07R | −0,21R a +0,06R | −0,18R a +0,03R | −0,21R (−0,32R a −0,08R) | 2 de 5 | Sin evidencia de ventaja |
| Todas las series | 1907 | −0,23R | −0,31R a −0,13R | −0,28R a −0,19R | −0,10R (−0,17R a −0,06R) | 2 de 20 | Sin evidencia de ventaja |

### 2.10 Entradas frente a salidas (mismas entradas con las salidas mecánicas; IC 95 % por bloques)

| Grupo | NEXUS | Operaciones | R con sus salidas | R con salidas mecánicas | Entradas sobre el azar (mismas salidas) | Efecto de las salidas del modelo |
| --- | --- | --- | --- | --- | --- | --- |
| Temporalidad 15m | con filtro | 26 | −0,14R | −0,07R | +0,03R (−0,32R a +0,24R · muy incierto) | −0,07R (−0,25R a +0,09R · muy incierto) |
| Temporalidad 15m | sin filtro | 500 | −0,42R | −0,48R | −0,09R (−0,19R a +0,02R) | +0,06R (+0,01R a +0,11R) |
| Temporalidad 1h | con filtro | 57 | −0,13R | −0,11R | −0,03R (−0,36R a +0,33R) | −0,02R (−0,21R a +0,13R) |
| Temporalidad 1h | sin filtro | 526 | −0,23R | −0,24R | −0,08R (−0,23R a +0,05R) | 0,00R (−0,06R a +0,06R) |
| Temporalidad 4h | con filtro | 156 | −0,17R | −0,13R | −0,10R (−0,28R a +0,08R) | −0,03R (−0,11R a +0,06R) |
| Temporalidad 4h | sin filtro | 566 | −0,16R | −0,15R | −0,11R (−0,21R a +0,01R) | −0,01R (−0,06R a +0,04R) |
| Temporalidad 1d | con filtro | 198 | 0,00R | −0,03R | −0,19R (−0,36R a −0,01R) | +0,03R (−0,04R a +0,11R) |
| Temporalidad 1d | sin filtro | 315 | −0,07R | −0,09R | −0,23R (−0,37R a −0,08R) | +0,02R (−0,05R a +0,08R) |
| Todas las series | con filtro | 437 | −0,08R | −0,08R | −0,12R (−0,24R a 0,00R) | 0,00R (−0,06R a +0,04R) |
| Todas las series | sin filtro | 1907 | −0,23R | −0,25R | −0,12R (−0,18R a −0,07R) | +0,02R (−0,02R a +0,04R) |

---------------------------------------------------------------------------------------------------------------------
## 3. Conclusión honesta

**No hay evidencia de que NEXUS + filtro tenga ventaja** en estos cinco pares de Binance y cuatro temporalidades, con
el motor final de la ronda de mejoras. Lo que los datos sí permiten decir, con la incertidumbre indicada:

1. **Sin filtro, las señales de NEXUS pierden después de costes** (−0,23R por operación en 1.907 operaciones, con el
   intervalo entero por debajo de 0) y sus entradas rinden menos que entradas al azar con las mismas salidas (−0,12R;
   IC −0,18R a −0,07R). Tal como está configurada, la señal no aporta información útil para entrar.
2. **El filtro funciona sobre todo como freno**: rechaza la gran mayoría de las señales (en 15m casi todas, porque la
   comisión se come el recorrido esperado) y así pierde menos que NEXUS sin filtro. Pero lo que sí toma tampoco
   supera al azar: en conjunto queda 0,13R por operación por debajo (IC 95 % −0,22R a −0,03R), y en 4h pierde con el
   intervalo entero por debajo de 0.
3. Ninguna serie, temporalidad ni periodo muestra un resultado favorable que se repita. Los tramos positivos
   (por ejemplo BTCUSDT 1h con +0,27R en 5 operaciones o ETHUSDT 1d con +0,12R en 67) tienen muestras pequeñas o
   intervalos que incluyen el 0.
4. Las cinco criptomonedas se mueven juntas (≈ 1,4 activos independientes): repetir la prueba con más pares cripto
   aportaría poca evidencia nueva.
5. **El motor final no cambia la conclusión** de la ejecución anterior (sección 7): las cifras principales del
   conjunto (expectativa, intervalo y exceso sobre el azar, con y sin filtro) se mueven una centésima de R como mucho y
   el veredicto es el mismo. El reparto entre entradas y salidas cambia algo más (hasta 0,04R) y por temporalidad los
   cambios son mayores (4h empeora; 15m mejora, pero con muy pocas operaciones).

Qué no se puede afirmar: que NEXUS gane, que el filtro «elija bien» o que el resultado vaya a repetirse. Tampoco que la
idea sea inútil en cualquier mercado o configuración: solo se midió la configuración por defecto, en cripto y en
estos periodos.

Posibles líneas de investigación (sin promesa de resultado; cada una exigiría una validación nueva fuera de muestra):
operar solo en temporalidades donde el coste sea pequeño frente al recorrido (4h y 1d), averiguar por qué las entradas
rinden menos que el azar con las mismas salidas (¿entran tarde en los movimientos?), y validar en mercados con costes
más bajos o menos correlacionados (divisas, índices) cuando haya datos fiables.

---------------------------------------------------------------------------------------------------------------------
## 4. Qué significa para el uso de NEXUS

- **NEXUS es una herramienta de investigación y simulación**, no un sistema con ventaja demostrada. Sus señales
  (comprar / vender / mantener), sus niveles y el veredicto del filtro (TOMAR / ESPERAR / NO TOMAR) sirven para
  estudiar el mercado, practicar en la cuenta demo y probar ideas, no como base para operar con dinero real.
- **El filtro sobre todo reduce cuánto se opera**: con él hubo 437 operaciones frente a 1.907 sin él (rechazó 2.080
  señales en todo el historial, calentamiento incluido) y así pierde menos que tomarlas todas, pero lo que deja pasar
  no es mejor que entrar al azar con las mismas reglas de riesgo. Un TOMAR no significa «esta operación tiene
  ventaja».
- **No hay evidencia de que sus señales superen a entradas al azar**: ni con filtro ni sin él, ni en conjunto ni en
  ninguna serie con muestra suficiente. Los porcentajes y puntuaciones de la app describen lo que el modelo calcula,
  no una probabilidad comprobada de ganar.
- **Qué cambiaría esta conclusión**: una validación nueva (otra versión del motor u otros ajustes) que cumpla todas
  las condiciones del apartado 1.4 fuera de muestra. Aun así sería un indicio medido en datos pasados, nunca una
  garantía. La tarjeta «Validación walk-forward» de *Aprendizaje* permite repetirla con los datos del momento.

Nada de esto es asesoría financiera.

---------------------------------------------------------------------------------------------------------------------
## 5. Limitaciones

- **Un solo mercado**: cripto al contado en Binance, cinco pares muy correlacionados (correlación diaria media 0,63).
  No dice nada de acciones, divisas o futuros.
- **Periodos distintos por temporalidad**: 5.000 velas son unas 7 semanas en 15m, 7 meses en 1h, 2 años y 3 meses en 4h
  y todo el historial disponible en 1d (desde 2017-2020 según el par). El resultado depende del régimen de cada tramo.
- **Parámetros por defecto, a propósito**: no se probaron variantes; probar muchas y quedarse con la mejor sería
  sobreajuste. El umbral de entrada por defecto se calibró en su día para la frecuencia de señales en cripto 1h, no
  para el rendimiento.
- **Cortos sin coste de financiación**: en el contado no se puede vender en corto; en futuros o margen habría
  intereses o *funding*, así que los cortos simulados son algo optimistas.
- **Costes fijos**: 0,1 % por lado y 0,02 % de deslizamiento por ejecución, iguales en todas las variantes. Con
  comisiones más bajas todo mejoraría algo, también el azar.
- **El azar usa salidas mecánicas**: las salidas del modelo (anticipada, señal en contra, giro) necesitan el modelo y
  no se pueden simular al azar; por eso también se compara «mismas salidas» (las entradas de NEXUS con las salidas
  mecánicas). Las entradas al azar de una secuencia pueden solaparse en el tiempo (NEXUS solo tiene una posición a
  la vez); para la expectativa por operación no importa.
- **Filtro sin lo aprendido en vivo**: en la app, el filtro también usa los resultados reales que va verificando. Aquí
  parte de cero para que todo sea fuera de muestra.
- **Muestras pequeñas**: 15 de las 20 series tienen menos de 30 operaciones con filtro (6 de ellas menos de 10, sin
  intervalo).
- **Comparaciones múltiples**: con 20 series y varias variantes, algún resultado favorable aislado sería esperable
  por casualidad; no apareció ninguno que cumpliera las condiciones.
- **Comprar y mantener** arriesga todo el capital; NEXUS, un 1 % por operación: sus retornos no son comparables en
  riesgo.
- **Datos del 31/10/2026 (12:59 UTC)**: una nueva ejecución desplaza las ventanas (sobre todo 15m y 1h) y cambia algo
  los números.

---------------------------------------------------------------------------------------------------------------------
## 6. Cómo repetirla

- **Desde la app**: página *Aprendizaje* → tarjeta «Validación walk-forward: ¿hay ventaja real?» → *Validar ahora*
  (valores por defecto de Ajustes: `validation.symbols`, `validation.tfs`, `validation.bars`) u *Opciones* para elegir
  símbolos, temporalidades y velas. El trabajo corre en segundo plano, con progreso; el último informe se guarda en la
  base de datos (`model_state`, tipo `validation`) y sobrevive a los reinicios.
- **Terminal**: `validar btc eth sol --tf=1h,4h --bars=3000` · `validar estado` · `validar resultado`.
- **API**: `POST /api/validation/run {symbols?, tfs?, bars?}` (409 si ya hay una en curso) · `GET /api/validation/status`
  · `GET /api/validation/latest`.
- **Línea de comandos** (sin servidor, velas directas de las API públicas de los exchanges con el cliente HTTP de la
  app), desde `backend/`:

  ```bash
  PYTHONUTF8=1 .venv/Scripts/python -m app.validation.cli \
    --symbols BINANCE:BTCUSDT,BINANCE:ETHUSDT,BINANCE:SOLUSDT,BINANCE:XRPUSDT,BINANCE:BNBUSDT \
    --tfs 15m,1h,4h,1d --bars 5000 \
    --out .tmp/validation/informe.json --compact ../docs/validation/binance-AAAA-MM-DDTHH-MMZ.json \
    --markdown .tmp/validation/tablas.md
  ```

  Tardó unos 65 s para las 20 series. Las tablas de la sección 2 son la salida de `--markdown` de esta ejecución.
  La prueba `backend/tests/test_imp_validation_verify.py` comprueba que este documento cita las cifras del informe más
  reciente de `docs/validation/`.
- **Huella del motor medido** (desde `backend/`; con el mismo código da `0afc602bbbbf…`):

  ```bash
  for f in __init__ costs experts features fmt gate gate_checks learner levels macro_merge nexus nexus_current \
    nexus_decide nexus_finish nexus_loop nexus_out nexus_resume positions predict regime ta; do
    git hash-object app/engine/$f.py; done | sha1sum
  ```

El código está en `backend/app/validation/` (`walkforward.py` núcleo, `mechanics.py` salidas mecánicas y azar,
`stats.py` estadística, `aggregate.py` conjunto, `verdict.py` veredictos) y sus pruebas en
`backend/tests/test_imp_validation_*.py`.

---------------------------------------------------------------------------------------------------------------------
## 7. Ejecución anterior (motor del commit `8191643`)

Se conserva por transparencia: la primera ejecución publicada, del 31/10/2026 a las 05:47 UTC, con el mismo universo
(20 series), los mismos ajustes y el motor del commit `8191643`, antes de los cambios de la ronda de mejoras. Su
informe reducido sigue en [`docs/validation/binance-2026-10-31.json`](validation/binance-2026-10-31.json). Aquel
informe todavía mostraba intervalos con menos de 10 operaciones y su tabla de cuenta ponía la caída de la cuenta
(vela a vela) junto a la del azar (operaciones cerradas); este documento ya usa las reglas de la sección 1.3.

| Cifra | Commit `8191643` (05:47 UTC) | Motor final (12:59 UTC) |
| --- | --- | --- |
| Operaciones con filtro (largos / cortos) | 414 (286 / 128) | 437 (303 / 134) |
| Expectativa con filtro (IC 95 % por bloques) | −0,08R (−0,17R a +0,02R) | −0,08R (−0,18R a +0,02R) |
| Exceso sobre el azar con filtro (IC 95 %) | −0,12R (−0,22R a −0,02R) | −0,13R (−0,22R a −0,03R) |
| Operaciones sin filtro | 1.896 | 1.907 |
| Expectativa sin filtro (IC 95 % por bloques) | −0,23R (−0,30R a −0,13R) | −0,23R (−0,31R a −0,13R) |
| Exceso sobre su azar sin filtro (IC 95 %) | −0,10R (−0,18R a −0,06R) | −0,10R (−0,17R a −0,06R) |
| Series con 30 operaciones o más con filtro | 4 de 20 | 5 de 20 |
| Mejor percentil frente al azar | 77 (BTCUSDT 1h, 6 operaciones) | 82 (BTCUSDT 1h, 5 operaciones) |
| Por temporalidad con filtro: 15m / 1h / 4h / 1d | −0,38R (31) · −0,05R (60) · −0,12R (115) · −0,02R (208) | −0,14R (26) · −0,13R (57) · −0,17R (156) · 0,00R (198) |
| Veredicto | Sin evidencia de ventaja | Sin evidencia de ventaja |

En 1d los datos de las dos ejecuciones son los mismos (velas diarias cerradas hasta el 30/10/2026), así que sus
diferencias se deben solo al motor: SOLUSDT da las mismas operaciones (solo cambian milésimas por redondeo), XRPUSDT
las mismas con filtro y casi las mismas sin él, y con filtro BTCUSDT pasa de 44 a 45 operaciones, ETHUSDT de 70 a 67 y
BNBUSDT de 55 a 47 (−0,11R → −0,04R). En 15m, 1h y 4h, además del motor, la ventana se desplazó unas 7 horas.
