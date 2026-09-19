<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { api } from '../../api/client'
import { fmtBytes } from '../../utils/format'

echarts.use([LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const FEATURE_LABELS = { chat: '对话', image: '绘图', search: '搜索', ppt: 'PPT/PSD' }
const data = ref(null)
const storage = ref(null)
const trendRef = ref(null)
const pieRef = ref(null)
let trendChart = null
let pieChart = null
let timer = null

async function load() {
  data.value = await api.get('/api/admin/overview')
  renderCharts()
}

async function loadStorage() {
  try {
    storage.value = await api.get('/api/admin/storage')
  } catch {
    /* 存储卡片失败不阻塞仪表盘 */
  }
}

function renderCharts() {
  if (!data.value) return
  if (trendRef.value) {
    trendChart = trendChart || echarts.init(trendRef.value)
    const days = data.value.trend.map((t) => t.day.slice(5))
    trendChart.setOption({
      tooltip: { trigger: 'axis' },
      legend: { data: Object.values(FEATURE_LABELS) },
      grid: { left: 40, right: 20, top: 40, bottom: 30 },
      xAxis: { type: 'category', data: days },
      yAxis: { type: 'value' },
      series: Object.entries(FEATURE_LABELS).map(([key, label]) => ({
        name: label,
        type: 'line',
        smooth: true,
        data: data.value.trend.map((t) => t[key] || 0),
      })),
    })
  }
  if (pieRef.value) {
    pieChart = pieChart || echarts.init(pieRef.value)
    pieChart.setOption({
      tooltip: { trigger: 'item' },
      legend: { bottom: 0 },
      series: [
        {
          type: 'pie',
          radius: ['40%', '70%'],
          label: { formatter: '{b}: {c}' },
          data: data.value.feature_distribution.map((d) => ({ name: d.label, value: d.count })),
        },
      ],
    })
  }
}

function onResize() {
  trendChart?.resize()
  pieChart?.resize()
}

onMounted(() => {
  load()
  loadStorage()
  timer = setInterval(load, 30000)
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  clearInterval(timer)
  window.removeEventListener('resize', onResize)
  trendChart?.dispose()
  pieChart?.dispose()
})
</script>

<template>
  <div class="page">
    <h2 class="page-title">仪表盘</h2>
    <el-row :gutter="16" v-if="data">
      <el-col :span="4" :xs="12"><el-card><el-statistic title="总用户数" :value="data.total_users" /></el-card></el-col>
      <el-col :span="4" :xs="12"><el-card><el-statistic title="待审核" :value="data.pending_users" /></el-card></el-col>
      <el-col :span="4" :xs="12"><el-card><el-statistic title="今日请求" :value="data.today_requests" /></el-card></el-col>
      <el-col :span="4" :xs="12"><el-card><el-statistic title="今日成功率" :value="data.today_success_rate" suffix="%" /></el-card></el-col>
      <el-col :span="4" :xs="12"><el-card><el-statistic title="今日生图（张）" :value="data.today_images" /></el-card></el-col>
      <el-col :span="4" :xs="12"><el-card><el-statistic title="在途请求" :value="data.active_requests" /></el-card></el-col>
    </el-row>

    <el-card v-if="storage" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>系统存储</span>
          <el-button size="small" text type="primary" @click="$router.push('/admin/settings')">
            前往「系统设置 · 存储与清理」
          </el-button>
        </div>
      </template>
      <el-row :gutter="16" align="middle">
        <el-col :span="5" :xs="12"><el-statistic title="产物占用" :value="fmtBytes(storage.total_bytes)" /></el-col>
        <el-col :span="5" :xs="12"><el-statistic title="数据库体积" :value="fmtBytes(storage.db_bytes)" /></el-col>
        <el-col :span="14" :xs="24">
          <div style="display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px">
            <span class="text-muted">数据分区磁盘</span>
            <span>{{ fmtBytes(storage.disk.used) }} / {{ fmtBytes(storage.disk.total) }}</span>
          </div>
          <el-progress
            :percentage="Math.round((storage.disk.used / storage.disk.total) * 1000) / 10"
            :status="storage.disk.used / storage.disk.total > 0.9 ? 'exception' : storage.disk.used / storage.disk.total > 0.75 ? 'warning' : ''"
            :stroke-width="12"
          />
        </el-col>
      </el-row>
    </el-card>

    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="14" :xs="24">
        <el-card>
          <template #header>近 7 天调用趋势</template>
          <div ref="trendRef" style="height: 300px"></div>
        </el-card>
      </el-col>
      <el-col :span="10" :xs="24">
        <el-card style="margin-top: 0" class="pie-card">
          <template #header>近 30 天功能分布</template>
          <div ref="pieRef" style="height: 300px"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-card style="margin-top: 16px" v-if="data">
      <template #header>近 7 天活跃用户 TOP</template>
      <el-table :data="data.top_users" size="small">
        <el-table-column prop="username" label="用户名" />
        <el-table-column prop="count" label="调用次数" width="140" />
      </el-table>
      <el-empty v-if="!data.top_users.length" description="暂无数据" :image-size="60" />
    </el-card>
  </div>
</template>

<style scoped>
@media (max-width: 768px) {
  .pie-card {
    margin-top: 16px !important;
  }
}
</style>
